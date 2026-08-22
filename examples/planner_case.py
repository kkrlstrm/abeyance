"""A case that works out its own path, and a planner that cannot make it take forever.

    python examples/planner_case.py

No Postgres, no Fly, no API key, no model — `JSONFileStore` for the store and
`LocalProcessRunner` for the workers, so the only thing installed is abeyance. The "planner" is a
twenty-line subprocess that reads its brief and returns JSON; swap it for a model call and nothing
else about this changes.

What it demonstrates, in order:

1. Evidence lands and no rule matches it. The case is stuck in a way nobody wrote down.
2. A disposable planner is dispatched, reads the case, proposes two things, and dies.
3. One proposal is dispatched. The other is dropped before it costs anything, because it could
   not say what its answer would change.
4. The new evidence lands. A *second* planner — a different process, knowing nothing about the
   first except what is on the case — reads it and says the case is ready to decide.
5. The case goes to a person. The third planning round never happens, and could not have.

The last point is the one worth watching. `PlanBudget` is two integers, both checked against the
case's own rows, and neither of them is anything the planner can talk its way past.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abeyance import (PLAN_NEED, PLAN_TAG, Capability, CapabilityRegistry, CaseLoop,
                      ContributionKind, HUMAN_DECISION, Need, PlanBudget, Planner,
                      parse_plan, planner_capability)
from abeyance.adapters import JSONFileStore, LocalProcessRunner
from abeyance.warrant import CaseView

# --------------------------------------------------------------------------- the workers

WRITE = '''
def write(payload, summary, scope=None):
    import json, os, sys, time
    sys.path.insert(0, os.environ["ABEYANCE_REPO"])
    from abeyance.adapters import JSONFileStore
    JSONFileStore(os.environ["ABEYANCE_STORE_ROOT"]).put(
        os.environ["ABEYANCE_CONTRIBUTION_KIND"], os.environ["ABEYANCE_CONTRIBUTION_KEY"], {
            "id": os.environ["ABEYANCE_CONTRIBUTION_KEY"],
            "case_id": os.environ["ABEYANCE_CASE_ID"],
            "request_id": os.environ["ABEYANCE_REQUEST_ID"],
            "kind": os.environ["ABEYANCE_EXPECTS"],
            "actor": {"id": os.environ["ABEYANCE_ACTOR"], "kind": "worker",
                      "standing": [], "display": ""},
            "summary": summary, "payload": payload, "scope": scope or {},
            "provenance": {"host": "local", "at": int(time.time())},
            "dependencies": [], "supersedes": "", "created_epoch": int(time.time())})
'''

EVIDENCE_WORKER = WRITE + '''
import json, os
spec = json.loads(os.environ["ABEYANCE_SPEC"])
need = os.environ["ABEYANCE_NEED"]

if need == "campaign-performance":
    write({"client": spec.get("client", "Acme"), "sent": 14852, "bounce_pct": 1.04,
           "days_since_last_send": 17, "gone_quiet": True},
          "14852 sent, 1.04% pooled bounce, quiet 17 days", scope={"max_leads": 500})
else:
    # The sharper reading. One campaign is the problem, not the whole account.
    write({"worst_campaign": "reengage-q3", "worst_bounce_pct": 3.29, "pooled_bounce_pct": 1.04,
           "safe_to_resume": False},
          "one campaign at 3.29% bounce is dragging the pooled figure",
          scope={"max_leads": 100, "warm_up_required": True})
'''

# The planner. It calls no model — it reads the brief and branches — but everything it is handed
# and everything it returns is exactly what a model-backed one gets and gives.
PLANNER_WORKER = WRITE + '''
import json, os
brief = json.loads(os.environ["ABEYANCE_SPEC"])
seen = {e.get("need") for e in brief["case"]["evidence"]}
catalogue = {c["need"] for c in brief["capabilities"]}

if "deliverability-check" in seen:
    plan = {"assessment": "ready-for-decision",
            "rationale": "The pooled 1.04% hid one campaign at 3.29%. That is the whole question "
                         "and it is answered; a narrower wave is a judgment call, not a "
                         "measurement. Nothing further would change what we do.",
            "proposals": []}
else:
    plan = {"assessment": "needs-work",
            "rationale": "Quiet 17 days with a healthy pooled bounce rate. Pooled numbers hide "
                         "per-campaign damage, so the pooled figure is not yet an answer.",
            "proposals": [
                {"need": "deliverability-check",
                 "why": "the pooled rate may be hiding one bad campaign",
                 "changes_decision_if": "if any single campaign is over 3% we cut the wave to "
                                        "100 and warm up first",
                 "spec": {"client": "Acme", "window_days": 30}},
                # Perfectly reasonable-sounding, and it never runs: it cannot say what a different
                # answer would change. This is the proposal that makes cases take forever.
                {"need": "fit-score",
                 "why": "it would be good to understand the list quality too",
                 "changes_decision_if": "",
                 "spec": {"client": "Acme"}},
            ]}
write(plan, plan["rationale"][:80])
'''


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="abeyance-planner-"))
    repo = str(Path(__file__).resolve().parents[1])
    try:
        (root / "workers").mkdir()
        eviden = root / "workers" / "evidence.py"
        plan_w = root / "workers" / "planner.py"
        eviden.write_text(EVIDENCE_WORKER)
        plan_w.write_text(PLANNER_WORKER)

        store = JSONFileStore(root / "store")
        registry = CapabilityRegistry([
            Capability(name="campaign-evidence", image="python:3.12-slim",
                       entrypoint=("python3",), cmd=(str(eviden),),
                       produces=("campaign-performance",), emits=ContributionKind.EVIDENCE,
                       reach=("db-read",), app="workers-data", timeout_seconds=60,
                       description="Sends, bounces and last-send date for one client."),
            Capability(name="deliverability", image="python:3.12-slim",
                       entrypoint=("python3",), cmd=(str(eviden),),
                       produces=("deliverability-check",), emits=ContributionKind.EVIDENCE,
                       reach=("db-read",), app="workers-data", timeout_seconds=60,
                       description="Per-campaign bounce and complaint rates over 30 days."),
            Capability(name="fit-scorer", image="python:3.12-slim",
                       entrypoint=("python3",), cmd=(str(eviden),),
                       produces=("fit-score",), emits=ContributionKind.RECOMMENDATION,
                       reach=("public-internet",), app="workers-model", timeout_seconds=60,
                       description="Scores how well this list fits the offer."),
        ])

        # Two rounds, two proposals a round, three pieces of work across the whole case. The
        # planner is told all three numbers in its brief; none of them is enforced by telling it.
        planner = Planner(registry, budget=PlanBudget(max_plans=2, max_needs_per_plan=2,
                                                      max_planned_needs=3))
        registry.add(planner_capability(image="python:3.12-slim", app="workers-model",
                                        entrypoint=("python3",), cmd=(str(plan_w),),
                                        timeout_seconds=60))

        def env_for(cap, case, req):
            return {"ABEYANCE_REPO": repo, "ABEYANCE_STORE_ROOT": str(root / "store")}

        cases = CaseLoop("launches", store=store, registry=registry,
                         runner=LocalProcessRunner(), rules=planner.rules(), env_for=env_for)

        print("── the case ──────────────────────────────────────────────────────")
        case = cases.open(action="launch-campaign", subject_key="acme",
                          title="Re-engage dormant Acme leads",
                          needs=[Need("campaign-performance", spec={"client": "Acme"})],
                          context={"goal": "decide whether to re-engage, and how large a wave"})
        print(f"  {case.id}  needs={[r.need for r in case.requests]}")
        print("  No rules beyond the planner's two. Nothing here knows what step comes second.")

        print("\n── ticks ─────────────────────────────────────────────────────────")
        # A tick is a cron line; hours apart in production, and the worker has long since
        # finished. Here each one pauses long enough for a subprocess to exit.
        for i in range(14):
            time.sleep(0.35)
            report = cases.tick(case_id=case.id)[0]
            for rec in report.dispatch.records:
                if rec.action in ("dispatched", "redispatched"):
                    print(f"  {i:>2}  started    {rec.request_id:<24} {rec.detail}")
            if report.derivation and report.derivation.fired:
                for r in report.derivation.new_requests:
                    print(f"  {i:>2}  WARRANTED  {r.need:<24} by={r.warranted_by}")
            for esc in (report.escalations or []) + (report.dispatch.escalations or []):
                print(f"  {i:>2}  ESCALATION[{esc.kind.value}] {esc.detail[:60]}")

        print("\n── what each planner proposed, and what survived ──────────────────")
        final = cases.get(case.id)
        plan_ids = {r.id for r in final.requests if r.need == PLAN_NEED}
        for n, c in enumerate((c for c in cases.contributions(case.id)
                               if c.request_id in plan_ids), start=1):
            plan = parse_plan(c.payload, contribution_id=c.id)
            adopted = {r.need for r in final.requests if (r.spec or {}).get(PLAN_TAG) == c.id}
            print(f"  round {n}: {plan.assessment}")
            for p in plan.proposals:
                verdict = "→ dispatched" if p.need in adopted else "× dropped   "
                print(f"    {verdict} {p.need:<22} "
                      f"changes_decision_if: {p.changes_decision_if or '(blank)'}"[:96])
            if not plan.proposals:
                print("    (proposed nothing — the case was ready)")
        print("\n  `fit-score` never cost a container. It sounded reasonable and could not say")
        print("  what a different answer would change, and that is the whole check.")

        print("\n── the budget, from the case's own rows ───────────────────────────")
        status = planner.status(CaseView(final, cases.contributions(case.id)))
        print(f"  rounds used {status['rounds_used']}/{status['budget']['max_plans']}   "
              f"work added {status['needs_added']}/{status['budget']['max_planned_needs']}")
        print(f"  would plan again now? {status['would_plan_now']}   "
              f"({status['why_not'] or 'nothing stopping it'})")

        print("\n── where it ended up ─────────────────────────────────────────────")
        for c in cases.contributions(case.id):
            print(f"  [{c.kind.value:<14}] {c.actor.id:<18} {c.summary[:52]}")
        print(f"\n  requests: {[r.id for r in final.requests]}")
        print(f"  authority: {cases.authority(case.id).reason[:72]}")
        assert HUMAN_DECISION in [r.need for r in final.requests]
        assert len([r for r in final.requests if r.need == PLAN_NEED]) == 2

        print("\n  Two rounds of planning, one piece of derived work, and it is in front of a")
        print("  person. A third round is not something the planner declined to ask for — it is")
        print("  something the case has no room for, and nothing the planner writes can add any.")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
