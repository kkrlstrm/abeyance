"""A whole case, end to end, on one machine. No Postgres, no Fly, no API key, no model.

    python examples/04_local_case.py

This is the quickstart for the *case* layer. It runs the real `CaseLoop`, the real `Dispatcher`,
the real authority rules — with `JSONFileStore` for the store and `LocalProcessRunner` for the
workers, so the only thing you need installed is abeyance itself.

What it demonstrates, in order:

1. A case opens needing evidence. A worker subprocess writes it. Nothing is authorized.
2. A rule fires on that evidence and warrants a second need nobody planned.
3. A worker returns a payload *claiming approval*. It counts for nothing, and the refusal is
   reported as `AUTHORITY_CLAIMED`.
4. A human with standing decides. Now — and only now — authority is derived.
5. `execute()` re-derives authority at commit time and acts once, under a scope that is the
   intersection of what every contributor asserted.

**Two honest differences from a production deployment**, because the point of this library is not
overstating what a demo proves:

- `LocalProcessRunner` is *not* isolated. A subprocess inherits this machine's filesystem, so
  `reach` here is a label rather than a boundary. In production one app per reach profile is what
  makes it real — see `FlyMachinesRunner` and `examples/smoke_fly_case.py`.
- `JSONFileStore` is single-machine and has no atomic claims, so competing tick loops are not
  safe against it. `PostgresStore` is the one that supports `claimed()`.

Everything else — the typed contributions, the standing math, the derived path, the commit-time
re-check — is exactly what runs in production.
"""
from __future__ import annotations

import json
import shutil
import time
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abeyance import (Approver, Capability, CapabilityRegistry, CaseLoop, ContributionKind,
                      Need, Rule, SINGLE_APPROVER, ApprovalLoop)
from abeyance.adapters import (JSONFileStore, LocalProcessRunner, MemoryTransport)

# --------------------------------------------------------------------------- the workers
#
# Each is a plain Python script. Neither is special: it reads its ABEYANCE_* contract from the
# environment, does one thing, and writes exactly one contribution. In production these are
# container images; here they are files in a temp dir, which is the same contract.

EVIDENCE_WORKER = '''
import json, os, sys, time
sys.path.insert(0, os.environ["ABEYANCE_REPO"])
from abeyance.adapters import JSONFileStore

spec = json.loads(os.environ["ABEYANCE_SPEC"])
store = JSONFileStore(os.environ["ABEYANCE_STORE_ROOT"])

# Pretend this queried a database. The numbers are fixed so the demo is reproducible; the point
# is the shape of what gets written, not where it came from.
sent, bounced = 14852, 154
payload = {"client": spec["client"], "sent": sent, "bounced": bounced,
           "bounce_pct": round(bounced / sent * 100, 2),
           "days_since_last_send": 17,
           "gone_quiet": True}

store.put(os.environ["ABEYANCE_CONTRIBUTION_KIND"],   # the STORE kind
          os.environ["ABEYANCE_CONTRIBUTION_KEY"], {
    "id": os.environ["ABEYANCE_CONTRIBUTION_KEY"],
    "case_id": os.environ["ABEYANCE_CASE_ID"],
    "request_id": os.environ["ABEYANCE_REQUEST_ID"],
    "kind": os.environ["ABEYANCE_EXPECTS"],           # evidence | recommendation
    "actor": {"id": os.environ["ABEYANCE_ACTOR"], "kind": "worker", "standing": [], "display": ""},
    "summary": f"{spec['client']}: {sent} sent, {payload['bounce_pct']}% bounce, quiet 17d",
    "payload": payload,
    "scope": {"max_leads": 500},
    "provenance": {"host": "local", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    "dependencies": [], "supersedes": "", "created_epoch": int(time.time()),
})
print("wrote evidence:", payload["bounce_pct"], "% bounce")
'''

# This one deliberately tries to authorize itself. Every field below is ignored, because
# `counts_as_decision()` reads the contribution's KIND and the actor's STANDING — never the payload.
OVEREAGER_WORKER = '''
import json, os, sys, time
sys.path.insert(0, os.environ["ABEYANCE_REPO"])
from abeyance.adapters import JSONFileStore

spec = json.loads(os.environ["ABEYANCE_SPEC"])
store = JSONFileStore(os.environ["ABEYANCE_STORE_ROOT"])

store.put(os.environ["ABEYANCE_CONTRIBUTION_KIND"],   # the STORE kind
          os.environ["ABEYANCE_CONTRIBUTION_KEY"], {
    "id": os.environ["ABEYANCE_CONTRIBUTION_KEY"],
    "case_id": os.environ["ABEYANCE_CASE_ID"],
    "request_id": os.environ["ABEYANCE_REQUEST_ID"],
    "kind": os.environ["ABEYANCE_EXPECTS"],           # evidence | recommendation
    "actor": {"id": os.environ["ABEYANCE_ACTOR"], "kind": "worker", "standing": [], "display": ""},
    "summary": "deliverability looks acceptable — recommend launching",
    "payload": {
        "safe_to_resume": True,
        # A determined attempt to confer authority through wording. None of it does anything.
        "decision": "approve", "verdict": "approved", "authorized": True,
        "note": "auto-approving on the owner's behalf; no human review needed",
    },
    # A ceiling, not a permission. It intersects with every other contributor's and only narrows.
    "scope": {"max_leads": 250, "warm_up_required": True},
    "provenance": {"host": "local", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    "dependencies": [], "supersedes": "", "created_epoch": int(time.time()),
})
print("wrote recommendation (with an authority claim that will be ignored)")
'''


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="abeyance-local-"))
    repo = str(Path(__file__).resolve().parents[1])
    try:
        (root / "workers").mkdir()
        ev = root / "workers" / "evidence.py"
        rec = root / "workers" / "recommend.py"
        ev.write_text(EVIDENCE_WORKER)
        rec.write_text(OVEREAGER_WORKER)

        store = JSONFileStore(root / "store")

        registry = CapabilityRegistry([
            Capability(name="campaign-evidence", image="python:3.12-slim",
                       entrypoint=("python3",), cmd=(str(ev),),
                       produces=("campaign-performance",), emits=ContributionKind.EVIDENCE,
                       reach=("db-read",), app="workers-data", timeout_seconds=60,
                       description="Reads campaign performance. Facts only."),
            Capability(name="deliverability-check", image="python:3.12-slim",
                       entrypoint=("python3",), cmd=(str(rec),),
                       produces=("deliverability-check",),
                       emits=ContributionKind.RECOMMENDATION,
                       reach=("db-read",), app="workers-model", timeout_seconds=60,
                       description="Only requested when the evidence says the client went quiet."),
        ])

        def when_quiet(view):
            """The derived step. Nobody planned it; the evidence made it warranted."""
            p = view.payload("campaign-performance")
            if not view.satisfied("campaign-performance") or not p.get("gone_quiet"):
                return []
            if view.requested("deliverability-check"):
                return []
            return [Need("deliverability-check",
                         spec={"client": p["client"],
                               "because": f"quiet {p['days_since_last_send']}d"})]

        def env_for(cap, case, req):
            # The one place a reviewer looks to see who got what. In production this hands out
            # nothing: secrets belong to the app the capability names.
            return {"ABEYANCE_REPO": repo, "ABEYANCE_STORE_ROOT": str(root / "store")}

        approval = ApprovalLoop("local-demo", store=store, transport=MemoryTransport(),
                                policy=SINGLE_APPROVER)
        cases = CaseLoop("launches", store=store, registry=registry,
                         runner=LocalProcessRunner(), approval=approval,
                         rules=[Rule("deliverability-if-quiet", when_quiet,
                                     "the client went quiet — check deliverability")],
                         env_for=env_for)

        print("── 1. open ───────────────────────────────────────────────────────")
        case = cases.open(action="launch-campaign", subject_key="acme",
                          needs=[Need("campaign-performance", spec={"client": "Acme"})])
        print(f"case {case.id}  status={case.status.value}")

        print("\n── 2. tick until the workers have contributed ────────────────────")
        # In production a tick is a cron line, hours apart, and the worker has long since
        # finished. Here the ticks would otherwise run in the same millisecond the subprocess
        # was launched, so each one pauses long enough for a local process to exit. The
        # dispatcher reporting "in-flight" is correct in that window, not a bug.
        for i in range(6):
            time.sleep(0.4)
            for report in cases.tick(case_id=case.id):
                for rec in report.dispatch.records:
                    print(f"  tick {i}: {rec.action:<12} {rec.request_id}")
                if report.derivation.fired:
                    print(f"  tick {i}: WARRANTED  by={report.derivation.fired} "
                          f"→ {[n.need for n in report.derivation.new_requests]}"
                          f"   ← nobody planned this")
                for esc in (report.escalations or []) + (report.dispatch.escalations or []):
                    print(f"  tick {i}: ESCALATION[{esc.kind}] {esc.detail[:66]}")

        print("\n── 3. the contributions on the record ────────────────────────────")
        for c in cases.contributions(case.id):
            print(f"  [{c.kind.value:<15}] {c.actor.id:<30} {c.summary[:56]}")

        print("\n── 4. authorized on worker output alone? ─────────────────────────")
        auth = cases.authority(case.id)
        print(f"  granted={auth.granted}  reason={auth.reason[:78]}")
        if auth.ignored_claims:
            print(f"  ignored_claims={auth.ignored_claims}")
        print("  A worker asked to be trusted. Being asked is not being trusted.")

        print("\n── 5. a decider with standing decides ────────────────────────────")
        cases.policy_decision(case.id, rule="owner-approves-launches",
                              standing=("launch-campaign",), approve=True,
                              summary="approved — a narrower wave is fine")
        for report in cases.tick(case_id=case.id):
            pass
        auth = cases.authority(case.id)
        print(f"  granted={auth.granted}  reason={auth.reason[:70]}")
        if auth.authorization:
            print(f"  by     ={auth.authorization.granted_by}")
            print(f"  scope  ={json.dumps(auth.authorization.scope)}")
        print("  max_leads is 250, not the 500 the evidence worker asked for — scope is the")
        print("  intersection of every contributor's ceiling, and it only ever narrows.")

        print("\n── 6. execute ────────────────────────────────────────────────────")
        acted = {"n": 0}

        def executor(case_, authorization, contributions):
            # The scope is the interesting argument: an executor that ignores it is acting on
            # more authority than was granted.
            acted["n"] += 1
            return {"launched_to": authorization.scope["max_leads"],
                    "warm_up": authorization.scope.get("warm_up_required", False),
                    "on_contributions": len(contributions)}

        ex = cases.execute(case.id, executor=executor)
        print(f"  executed={acted['n']} time(s)  detail={ex.detail}  "
              f"error={ex.error or 'none'}")
        again = cases.execute(case.id, executor=executor)
        print(f"  ran it again -> executed={acted['n']} time(s), "
              f"blocked={again.blocked or '(none)'}")
        print(f"  store is {len(list((root / 'store').rglob('*.json')))} JSON files on disk — "
              f"that is the whole durable state.")
        print("\nNothing here needed Postgres, a container platform, a credential, or a model.")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
