"""Bridge: an existing eval-gated routing allowlist becomes a clearance registry.

`abeyance/clearance.py` says a model is cleared for specific contribution *kinds*, on recorded
evidence. It does not say where that evidence lives. This module wires one real allowlist —
`~/openrouter-test/routes.json`, an eval-gated model-routing policy — into a `ClearanceRegistry`,
so the two halves compose instead of duplicating each other:

    routes.json  (the measurement: which model, scored how, verified when)
         │
         ▼
    ClearanceRegistry  (the permission: which CONTRIBUTION KIND that score covers)
         │
         ▼
    model_capability()  (refuses a mis-kinded capability at registry-build time)

**routes.json stays the single source of truth.** It is read live when present, and an embedded
snapshot carries the policy when it is not (a distributed copy, a container, CI). `routes_consistency()`
asserts the two never drift — a fallback that can silently disagree with canonical is worse than no
fallback, because it fails in the direction of permitting more.

## The finding, which is the interesting part

Mapping the allowlist onto contribution kinds surfaced something the routing file could not say:
**every mode is EVIDENCE except `lint-code`, and none is cleared for a RECOMMENDATION on a
consequential action.** That is not an oversight. The policy behind routes.json explicitly
disqualifies drafting, single-row classification with consequences, primary architecture/security
review, and positioning critique from delegation — so what remains, by construction, is extraction,
filtering, digest, and vision. It has been an evidence allowlist from the start; the clearance layer
is just the first thing to name it.

The consequence is a clean split, and it is the one the case layer wants anyway:

    EVIDENCE        cheap, metered, eval-gated       → the OpenRouter rungs below
    RECOMMENDATION  the orchestrator tier            → ORCHESTRATOR_CLEARANCES below

routes.json governs delegation *away from* the orchestrator, so it contains no entry for the
orchestrator itself. The judgment clearance is therefore declared here, separately, and points at
its own eval — not at routes.json, which has nothing to say about it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from abeyance import ContributionKind
from abeyance.clearance import ClearanceRegistry, ModelClearance
from abeyance.errors import NotCleared

EVIDENCE = ContributionKind.EVIDENCE
RECOMMENDATION = ContributionKind.RECOMMENDATION

ROUTES_PATH = Path(os.environ.get(
    "ABEYANCE_ROUTES_JSON", Path.home() / "openrouter-test" / "routes.json"))

# --------------------------------------------------------------------------- the kind map
#
# The one judgement call in this file, so it is explicit rather than inferred, with the reasoning
# recorded per mode. A mode absent from this map has no clearance — adding a routing mode does NOT
# silently grant it a contribution kind, which is the whole point of keeping the map by hand.

KIND_FOR: Dict[str, Tuple[ContributionKind, ...]] = {
    # Assertions about the world: what a field says, what a document contains, what a page shows.
    # None of these form an opinion about what to do, so none can be a RECOMMENDATION.
    "extract-bulk":        (EVIDENCE,),
    "extract-accurate":    (EVIDENCE,),
    "extract-multimodal":  (EVIDENCE,),
    "digest-longcontext":  (EVIDENCE,),

    # A binary autoresponder/OOO classification is still an assertion about a message, not a
    # judgment about an action. Its own do_not_use_when bars multi-class and intent detection.
    "filter-auto-reply":   (EVIDENCE,),

    # The one RECOMMENDATION in the allowlist. A supplemental linter produces judgment without
    # authority — CASES.md names "a security review" as exactly that — and its own routing entry
    # says findings MUST be re-evaluated before action and it is never the primary review. That is
    # the definition of a recommendation, so it is cleared as one and cannot be read as fact.
    "lint-code":           (RECOMMENDATION,),
}

# --------------------------------------------------------------------------- embedded snapshot
#
# Mirrors the canonical file so the policy travels with a copy that ships without it. Keep in step
# with routes.json; `routes_consistency()` fails loudly when it drifts.

EMBEDDED_ROUTES: Dict[str, Any] = {
    "staleness_warn_days": 120,
    "modes": {
        "extract-bulk": {
            "model": "qwen/qwen3-235b-a22b-2507", "provider": None,
            "verified_date": "2026-05-18",
            "evidence_ref": "OBSERVATIONS.md 'Eval 1' (2026-05-18)",
            "do_not_use_when": "Operator will read a single output row and make a decision from "
                               "it. ~20% disagreement with the orchestrator on judgment-laden "
                               "fields.",
        },
        "filter-auto-reply": {
            "model": "google/gemini-2.5-flash-lite", "provider": None,
            "verified_date": "2026-05-18",
            "evidence_ref": "OBSERVATIONS.md 'Eval 6' (2026-05-18)",
            "do_not_use_when": "Any multi-class classification, intent detection, or judgment "
                               "task.",
        },
        "lint-code": {
            "model": "mistralai/codestral-2508", "provider": None,
            "verified_date": "2026-05-18",
            "evidence_ref": "OBSERVATIONS.md 'Eval 4' (2026-05-18)",
            "do_not_use_when": "The primary architecture/security review — it missed a SQL "
                               "injection the orchestrator caught in real production code.",
        },
        "digest-longcontext": {
            "model": "deepseek/deepseek-v4-flash", "provider": None,
            "verified_date": "2026-06-04",
            "evidence_ref": "OBSERVATIONS.md 'Eval 2b' (2026-06-04)",
            "do_not_use_when": "A high-stakes irreversible decision with no human review.",
        },
        "extract-accurate": {
            "model": "deepseek/deepseek-v4-flash", "provider": None,
            "verified_date": "2026-06-04",
            "evidence_ref": "OBSERVATIONS.md 'Eval 1b' (2026-06-04)",
            "do_not_use_when": "Maximum-throughput synchronous batches, and single-row decisions "
                               "a human acts on directly.",
        },
        "extract-multimodal": {
            "model": "minimax/minimax-m3", "provider": None,
            "verified_date": "2026-06-25",
            "evidence_ref": "OBSERVATIONS.md 'Eval 8' (2026-06-25)",
            "do_not_use_when": "The page is text-extractable, or a per-row value is acted on "
                               "without a never-invent-a-value verify.",
        },
    },
    # Retired with a dated reason, never silently deleted — so a request for one is refused with
    # the reason rather than a bare "unknown mode", and nobody re-adds it by accident.
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

# --------------------------------------------------------------------------- orchestrator tier
#
# NOT from routes.json, which governs delegation away from the orchestrator and therefore has no
# entry for it. Declared here so the RECOMMENDATION that actually carries a case is as reviewable
# as the cheap rungs — and so `evidence_ref` points at a real run before you trust it.

ORCHESTRATOR_CLEARANCES: List[ModelClearance] = [
    ModelClearance(
        mode="case-recommendation",
        model="claude-opus-5",
        emits=(RECOMMENDATION,),
        evidence_ref="evals/case-recommendation.md — verdicts vs recorded human decisions "
                     "(REPLACE with your own recorded run before relying on this)",
        verified_date="2026-08-17",
        stale_after_days=120,
        notes="Judgment on a consequential action, reviewed by a human before anything executes. "
              "Runs on a subscription credential via the Claude Code CLI, so it is flat-rate "
              "rather than metered. NOT for bulk extraction — the expensive way to read a page.",
    ),
]


# --------------------------------------------------------------------------- loading


def load_routes() -> Tuple[Dict[str, Any], str]:
    """`(routes, source)` — canonical file when readable, else the embedded snapshot."""
    try:
        raw = json.loads(ROUTES_PATH.read_text())
        if raw.get("modes"):
            return raw, str(ROUTES_PATH)
    except Exception:      # noqa: BLE001 — any read/parse problem falls back to embedded
        pass
    return EMBEDDED_ROUTES, "embedded"


def routes_consistency() -> Tuple[bool, str]:
    """Assert the embedded snapshot still agrees with canonical routes.json.

    A drifting fallback is worse than none: it fails toward permitting a model the canonical policy
    has re-pointed or retired. Wire this into a test or a preflight, not into a comment.
    """
    if not ROUTES_PATH.is_file():
        return True, "no canonical routes.json present — embedded is authoritative"
    try:
        canon = json.loads(ROUTES_PATH.read_text())
    except Exception as e:      # noqa: BLE001
        return False, f"canonical routes.json unreadable: {e.__class__.__name__}"

    cm = {k: v["model"] for k, v in (canon.get("modes") or {}).items()}
    em = {k: v["model"] for k, v in EMBEDDED_ROUTES["modes"].items()}

    # Both problems are reported in one pass. A newly-added canonical mode is *two* findings —
    # the snapshot is stale AND the mode has no contribution kind — and surfacing them one at a
    # time means you fix the first, re-run, and only then learn about the second.
    problems: List[str] = []

    if cm != em:
        only_canon = {k: cm[k] for k in cm.keys() - em.keys()}
        only_emb = {k: em[k] for k in em.keys() - cm.keys()}
        changed = {k: (em[k], cm[k]) for k in cm.keys() & em.keys() if cm[k] != em[k]}
        problems.append(f"DRIFT — update EMBEDDED_ROUTES. canonical-only={only_canon} "
                        f"embedded-only={only_emb} changed={changed}")

    unmapped = sorted(cm.keys() - set(KIND_FOR))
    if unmapped:
        problems.append(f"modes with no contribution kind: {unmapped}. Add them to KIND_FOR with "
                        f"the reasoning, or they get no clearance at all.")

    if problems:
        return False, " | ".join(problems)
    return True, f"embedded matches canonical, all {len(cm)} modes mapped to a kind"


def build_clearances(*, include_orchestrator: bool = True) -> ClearanceRegistry:
    """Every allowlisted routing mode as a clearance, plus the orchestrator tier.

    A mode present in routes.json but absent from `KIND_FOR` is skipped, not guessed — a new
    routing mode must be given a contribution kind deliberately.
    """
    routes, _ = load_routes()
    default_stale = int(routes.get("staleness_warn_days") or 120)

    reg = ClearanceRegistry()
    for mode, spec in (routes.get("modes") or {}).items():
        kinds = KIND_FOR.get(mode)
        if not kinds:
            continue
        notes = spec.get("do_not_use_when") or spec.get("purpose") or ""
        reg.add(ModelClearance(
            mode=mode,
            model=spec["model"],
            emits=kinds,
            evidence_ref=spec.get("evidence_ref") or f"routes.json:{mode}",
            verified_date=spec["verified_date"],
            provider=(spec.get("provider") or {}).get("order", [""])[0]
                     if isinstance(spec.get("provider"), dict) else "",
            stale_after_days=int(spec.get("stale_after_days") or default_stale),
            notes=f"do_not_use_when: {notes}" if notes else "",
        ))
    if include_orchestrator:
        for c in ORCHESTRATOR_CLEARANCES:
            reg.add(c)
    return reg


def retired_reason(mode: str) -> Optional[str]:
    routes, _ = load_routes()
    r = routes.get("retired") or {}
    entry = r.get(mode)
    if entry is None:
        return None
    return entry if isinstance(entry, str) else (entry.get("reason") or "retired")


def require(reg: ClearanceRegistry, mode: str, kind: ContributionKind,
            *, today: str = "") -> ModelClearance:
    """`reg.require`, but a retired mode is refused with the reason it was retired.

    An unknown mode and a deliberately-retired one are different facts. Collapsing them into
    "unknown mode" is how a mode that failed an eval gets quietly re-added a year later.
    """
    reason = retired_reason(mode)
    if reason:
        raise NotCleared(f"mode {mode!r} was retired: {reason} Retired modes are refused by "
                         f"name, not forgotten — re-clearing one needs a passing eval.")
    return reg.require(mode, kind, today=today)


CLEARANCES = build_clearances()


if __name__ == "__main__":
    routes, source = load_routes()
    ok, msg = routes_consistency()
    print(f"source     : {source}")
    print(f"consistency: {'ok' if ok else 'FAIL'} — {msg}\n")

    print(f"{'mode':<22} {'kind':<15} {'model':<34} verified")
    for c in CLEARANCES.all():
        print(f"{c.mode:<22} {'/'.join(k.value for k in c.emits):<15} {c.model:<34} "
              f"{c.verified_date}")

    print("\nclearance_report() — what may form which kind of contribution:")
    print(json.dumps(CLEARANCES.clearance_report(), indent=2))

    today = os.environ.get("ABEYANCE_TODAY", "")
    if today:
        stale = CLEARANCES.stale(today)
        print(f"\nstale as of {today}: "
              f"{[f'{c.mode} ({c.age_days(today)}d)' for c in stale] or 'none'}")
