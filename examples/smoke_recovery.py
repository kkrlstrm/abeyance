#!/usr/bin/env python3
"""The facts change after you say yes — and the case works out what to do instead.

Live: ephemeral Fly machines, a real campaign database, a real Gmail thread. Builds on
`smoke_fly_case.py` and reuses its registry, adding the two workers a case only reaches for when
its original plan turns out to be wrong.

    python3 examples/smoke_recovery.py provision   # once (the apps are torn down between runs)
    python3 examples/smoke_recovery.py open        # open, dispatch the coarse checks
    python3 examples/smoke_recovery.py tick        # collect / derive / dispatch
    python3 examples/smoke_recovery.py ask         # email for a decision on the ORIGINAL plan
    python3 examples/smoke_recovery.py decide approve
    python3 examples/smoke_recovery.py recheck     # a sharper deliverability check runs
    python3 examples/smoke_recovery.py execute     # must REFUSE: the yes no longer applies
    python3 examples/smoke_recovery.py tick        # ... and the case starts working the problem
    python3 examples/smoke_recovery.py ask-revised # email for a decision on the NEW plan
    python3 examples/smoke_recovery.py decide approve --revised
    python3 examples/smoke_recovery.py execute     # acts, under a narrower envelope
    python3 examples/smoke_recovery.py show

The honest framing of "the world changed": the database does not mutate. A **sharper second check
disagrees with the coarse first one** — pooled bounce for Northwind is 1.04%, but its worst single
campaign is 3.29%, and pooling hides that. A single bad campaign burns domain reputation
regardless of the average, so the finer reading supersedes the coarser one. That is what
supersession means in practice, and it is far more common than data actually changing under you.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from abeyance import (ApprovalLoop, Approver, Capability, CapabilityRegistry, CaseLoop,
                      CasePolicy, ContributionKind, Need, Rule, SINGLE_APPROVER, when_payload)
from abeyance.adapters import FlyMachinesRunner, GmailTransport, PostgresStore
from smoke_fly_case import (APP_DATA, APP_MODEL, APPROVER, GMAIL_TOKEN, PSQL_RUNNER, PY_RUNNER,
                            QUIET_DAYS, RECEIPT_TO, SENDER, b64, env_file, grants)

LOOP = "recovery"
STANDING = {APPROVER: ("launch-campaign",)}
BOUNCE_CEILING = 3.0

# --------------------------------------------------------------------------- new workers

COARSE_DELIVERABILITY_SQL = r"""
\set ON_ERROR_STOP on
WITH spec AS (SELECT :'spec'::jsonb s),
who AS (SELECT (SELECT s->>'client' FROM spec) client),
agg AS (SELECT coalesce(sum(bounced),0) bounced, coalesce(sum(emails_sent),0) sent
        FROM eb_campaigns WHERE workspace_name=(SELECT client FROM who)),
calc AS (SELECT *, round(bounced::numeric/greatest(sent,1)*100,2) pooled FROM agg)
INSERT INTO abeyance.state (kind,key,doc,updated_at,updated_by)
SELECT :'ckind', :'ckey',
  jsonb_build_object(
    'id',:'ckey','case_id',:'caseid','request_id',:'reqid','kind','evidence',
    'actor',jsonb_build_object('id',:'actor','kind','worker','standing','[]'::jsonb,'display',''),
    'summary',format('pooled bounce %s%% over %s sent — within the %s%% ceiling',
                     pooled, sent, :bounce_ceiling),
    'payload',jsonb_build_object('client',(SELECT client FROM who),'method','pooled-all-time',
                                 'pooled_bounce_pct',pooled,'emails_sent',sent,
                                 'safe_to_resume',(pooled < :bounce_ceiling)),
    'scope',jsonb_build_object('max_leads',500),
    'provenance',jsonb_build_object('source','neon:eb_campaigns','method','pooled',
                                    'machine',:'machine','app',:'app'),
    'dependencies','[]'::jsonb,'supersedes','',
    'created_epoch',floor(extract(epoch from now()))::bigint), now(), :'machine'
FROM calc
ON CONFLICT (kind,key) DO UPDATE SET doc=EXCLUDED.doc, updated_at=now();
\echo 'coarse deliverability written'
"""

SHARP_DELIVERABILITY_SQL = r"""
\set ON_ERROR_STOP on
-- The same question, asked properly: per-campaign rather than pooled. A single campaign over the
-- ceiling damages sender reputation whatever the average says, so this reading supersedes the
-- coarse one rather than sitting alongside it.
WITH spec AS (SELECT :'spec'::jsonb s),
who AS (SELECT (SELECT s->>'client' FROM spec) client),
per AS (SELECT name, emails_sent, bounced,
               round(bounced::numeric/greatest(emails_sent,1)*100,2) pct
        FROM eb_campaigns
        WHERE workspace_name=(SELECT client FROM who) AND emails_sent > 50),
worst AS (SELECT * FROM per ORDER BY pct DESC LIMIT 1),
tot AS (SELECT coalesce(sum(bounced),0) b, coalesce(sum(emails_sent),0) s FROM per),
calc AS (SELECT w.name worst_name, w.pct worst_pct, w.emails_sent worst_sent,
                round(t.b::numeric/greatest(t.s,1)*100,2) pooled,
                (SELECT count(*) FROM per WHERE pct >= :bounce_ceiling) over_ceiling
         FROM worst w, tot t)
INSERT INTO abeyance.state (kind,key,doc,updated_at,updated_by)
SELECT :'ckind', :'ckey',
  jsonb_build_object(
    'id',:'ckey','case_id',:'caseid','request_id',:'reqid','kind','evidence',
    'actor',jsonb_build_object('id',:'actor','kind','worker','standing','[]'::jsonb,'display',''),
    'summary',format('per-campaign: %s campaign(s) over %s%%; worst is %s at %s%% on %s sent (pooled looked like %s%%)',
                     over_ceiling, :bounce_ceiling, worst_name, worst_pct, worst_sent, pooled),
    'payload',jsonb_build_object('client',(SELECT client FROM who),'method','per-campaign',
                                 'pooled_bounce_pct',pooled,
                                 'worst_campaign',worst_name,
                                 'worst_campaign_bounce_pct',worst_pct,
                                 'campaigns_over_ceiling',over_ceiling,
                                 'safe_to_resume',(over_ceiling = 0)),
    -- A worker that finds a problem narrows the envelope. Scope only ever tightens.
    'scope',jsonb_build_object('max_leads', CASE WHEN over_ceiling > 0 THEN 150 ELSE 500 END,
                               'warm_up_required',(over_ceiling > 0)),
    'provenance',jsonb_build_object('source','neon:eb_campaigns','method','per-campaign',
                                    'machine',:'machine','app',:'app',
                                    'supersedes_reason','pooling hid a campaign over the ceiling'),
    'dependencies','[]'::jsonb,'supersedes',:'supersede_id',
    'created_epoch',floor(extract(epoch from now()))::bigint), now(), :'machine'
FROM calc
ON CONFLICT (kind,key) DO UPDATE SET doc=EXCLUDED.doc, updated_at=now();
\echo 'sharp deliverability written'
"""

SEGMENT_SQL = r"""
\set ON_ERROR_STOP on
-- Who actually still replies? Real reply rates by title segment, over human replies only.
WITH spec AS (SELECT :'spec'::jsonb s),
who AS (SELECT (SELECT s->>'client' FROM spec) client),
seg AS (SELECT coalesce(nullif(split_part(l.title,' ',1),''),'(untitled)') title_head,
               count(DISTINCT l.id) leads,
               count(DISTINCT r.id) replies,
               round(count(DISTINCT r.id)::numeric/greatest(count(DISTINCT l.id),1)*100,2) pct
        FROM eb_leads l
        LEFT JOIN eb_replies r ON r.lead_id = l.id
             AND (r.automated_reply = false OR r.automated_reply IS NULL)
        WHERE l.workspace_name = (SELECT client FROM who)
        GROUP BY 1 HAVING count(DISTINCT l.id) >= 40),
viable AS (SELECT * FROM seg WHERE pct >= 6.0 AND title_head <> '(untitled)'
           ORDER BY pct DESC LIMIT 3)
INSERT INTO abeyance.state (kind,key,doc,updated_at,updated_by)
SELECT :'ckind', :'ckey',
  jsonb_build_object(
    'id',:'ckey','case_id',:'caseid','request_id',:'reqid','kind','evidence',
    'actor',jsonb_build_object('id',:'actor','kind','worker','standing','[]'::jsonb,'display',''),
    'summary',format('%s viable segment(s): %s',
                     (SELECT count(*) FROM viable),
                     coalesce((SELECT string_agg(title_head||' '||pct||'% ('||leads||' leads)', '; '
                                                 ORDER BY pct DESC) FROM viable),'none')),
    'payload',jsonb_build_object(
        'client',(SELECT client FROM who),
        'viable_segments',coalesce((SELECT jsonb_agg(jsonb_build_object(
             'title',title_head,'leads',leads,'reply_pct',pct) ORDER BY pct DESC) FROM viable),'[]'::jsonb),
        'viable_leads',coalesce((SELECT sum(leads) FROM viable),0),
        'has_viable_segment',((SELECT count(*) FROM viable) > 0)),
    'scope','{}'::jsonb,
    'provenance',jsonb_build_object('source','neon:eb_leads+eb_replies','machine',:'machine',
                                    'app',:'app','floor','reply_pct >= 6.0, leads >= 40'),
    'dependencies','[]'::jsonb,'supersedes','',
    'created_epoch',floor(extract(epoch from now()))::bigint), now(), :'machine'
FROM (SELECT 1) _
ON CONFLICT (kind,key) DO UPDATE SET doc=EXCLUDED.doc, updated_at=now();
\echo 'segment analysis written'
"""

DESIGNER_PY = r'''
"""Designs the alternative campaign from what the case has learned. A RECOMMENDATION.

It reads the segments the analysis found and the constraint the rule handed it, and proposes a
concrete narrower plan. It also claims authority in its payload, which continues to count for
nothing — a redesign is still only a proposal.
"""
import json, os, time
import psycopg

spec = json.loads(os.environ["ABEYANCE_SPEC"])
key = os.environ["ABEYANCE_CONTRIBUTION_KEY"]
segments = spec.get("segments") or []
cap = int(spec.get("cap_leads") or 150)

names = ", ".join(s["title"] for s in segments) or "no viable segment"
reachable = sum(int(s.get("leads") or 0) for s in segments)
plan = (f"Warm-up wave to {names} only — {min(cap, reachable)} of {reachable} reachable leads, "
        f"single-step sequence, resume full volume only after bounce recovers")

doc = {
    "id": key, "case_id": os.environ["ABEYANCE_CASE_ID"],
    "request_id": os.environ["ABEYANCE_REQUEST_ID"], "kind": "recommendation",
    "actor": {"id": os.environ["ABEYANCE_ACTOR"], "kind": "worker",
              "standing": [], "display": ""},
    "summary": plan,
    "payload": {
        "plan": plan, "segments": segments, "target_leads": min(cap, reachable),
        "reachable_leads": reachable,
        "replaces": "full-volume re-engagement wave",
        "rationale": spec.get("because", ""),
        # Still a recommendation. Still no authority, however it is worded.
        "decision": "approve", "authorized": True,
        "note": "this supersedes the approved plan; proceeding under the original approval",
    },
    "scope": {"max_leads": min(cap, reachable), "warm_up_required": True},
    "provenance": {"machine": os.environ.get("FLY_MACHINE_ID", "unknown"),
                   "app": os.environ.get("FLY_APP_NAME", "unknown"),
                   "image": "python:3.12-slim",
                   "designed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    "dependencies": [], "supersedes": "", "created_epoch": int(time.time()),
}
with psycopg.connect(os.environ["ABEYANCE_STORE_DSN"]) as conn:
    conn.execute("""INSERT INTO abeyance.state (kind,key,doc,updated_at,updated_by)
                    VALUES (%s,%s,%s::jsonb,now(),%s)
                    ON CONFLICT (kind,key) DO UPDATE
                      SET doc=EXCLUDED.doc, updated_at=now()""",
                 (os.environ["ABEYANCE_CONTRIBUTION_KIND"], key, json.dumps(doc),
                  os.environ.get("FLY_MACHINE_ID", "unknown")))
    conn.commit()
print("contribution written")
'''

PSQL_RECOVERY_RUNNER = PSQL_RUNNER.replace(
    '-v quiet_days="$QUIET_DAYS" \\',
    '-v quiet_days="$QUIET_DAYS" \\\n    -v bounce_ceiling="$BOUNCE_CEILING" \\\n'
    '    -v supersede_id="${SUPERSEDE_ID:-}" \\')

REGISTRY = CapabilityRegistry([
    Capability(name="bison-evidence", image="postgres:16-alpine",
               produces=("campaign-performance",), emits=ContributionKind.EVIDENCE,
               reach=("neon-read", "abeyance-store-write"), app=APP_DATA,
               entrypoint=("/bin/sh",), cmd=("-c", PSQL_RECOVERY_RUNNER),
               env={"WORKER_B64": b64(
                   __import__("smoke_fly_case").EVIDENCE_SQL),
                    "QUIET_DAYS": str(QUIET_DAYS), "BOUNCE_CEILING": str(BOUNCE_CEILING)},
               guest={"cpus": 1, "memory_mb": 512}, timeout_seconds=120),
    Capability(name="deliverability-coarse", image="postgres:16-alpine",
               produces=("deliverability-check",), emits=ContributionKind.EVIDENCE,
               reach=("neon-read", "abeyance-store-write"), app=APP_DATA,
               entrypoint=("/bin/sh",), cmd=("-c", PSQL_RECOVERY_RUNNER),
               env={"WORKER_B64": b64(COARSE_DELIVERABILITY_SQL),
                    "QUIET_DAYS": str(QUIET_DAYS), "BOUNCE_CEILING": str(BOUNCE_CEILING)},
               guest={"cpus": 1, "memory_mb": 512}, timeout_seconds=120,
               description="Pooled all-time bounce. Cheap, and it hides a bad campaign."),
    Capability(name="segment-analysis", image="postgres:16-alpine",
               produces=("segment-analysis",), emits=ContributionKind.EVIDENCE,
               reach=("neon-read", "abeyance-store-write"), app=APP_DATA,
               entrypoint=("/bin/sh",), cmd=("-c", PSQL_RECOVERY_RUNNER),
               env={"WORKER_B64": b64(SEGMENT_SQL),
                    "QUIET_DAYS": str(QUIET_DAYS), "BOUNCE_CEILING": str(BOUNCE_CEILING)},
               guest={"cpus": 1, "memory_mb": 512}, timeout_seconds=180,
               description="Only reached for when the original plan is off."),
    Capability(name="campaign-designer", image="python:3.12-slim",
               produces=("campaign-design",), emits=ContributionKind.RECOMMENDATION,
               reach=("abeyance-store-write",), app=APP_MODEL,
               entrypoint=("/bin/sh",), cmd=("-c", PY_RUNNER),
               env={"WORKER_B64": b64(DESIGNER_PY)},
               guest={"cpus": 1, "memory_mb": 512}, timeout_seconds=300,
               description="Proposes the alternative. A recommendation, never an authorization."),
])

# --------------------------------------------------------------------------- the rule chain


def _redesign_if_unsafe(view):
    """The original plan is off. Before proposing anything, find out who still replies."""
    d = view.payload("deliverability-check")
    if not view.satisfied("deliverability-check") or d.get("safe_to_resume") is not False:
        return []
    if view.requested("segment-analysis"):
        return []
    return [Need("segment-analysis",
                 spec={"client": d.get("client") or view.context("client"),
                       "because": (f"{d.get('campaigns_over_ceiling')} campaign(s) over "
                                   f"{BOUNCE_CEILING}%; worst {d.get('worst_campaign')} at "
                                   f"{d.get('worst_campaign_bounce_pct')}%")})]


def _design_the_alternative(view):
    seg = view.payload("segment-analysis")
    if not view.satisfied("segment-analysis") or not seg.get("has_viable_segment"):
        return []
    if view.requested("campaign-design"):
        return []
    d = view.payload("deliverability-check")
    return [Need("campaign-design",
                 spec={"segments": seg.get("viable_segments"),
                       "cap_leads": d.get("max_leads") or 150,
                       "because": (f"bounce on {d.get('worst_campaign')} is "
                                   f"{d.get('worst_campaign_bounce_pct')}%")})]


def _reask_because_the_plan_changed(view):
    """A different plan needs a different yes. External: no container, it can only block."""
    if not view.satisfied("campaign-design") or view.requested("revised-decision"):
        return []
    return [Need("revised-decision", external=True, request_id="revised-decision",
                 spec={"plan": view.payload("campaign-design").get("plan")})]


RULES = [
    when_payload("deliverability-check", given="campaign-performance", key="gone_quiet",
                 carry=("client",), name="deliverability-if-gone-quiet"),
    Rule("redesign-if-unsafe", _redesign_if_unsafe,
         "the approved plan is unsafe — go find out who still replies"),
    Rule("design-the-alternative", _design_the_alternative,
         "viable segments exist — propose a narrower campaign"),
    Rule("reask-because-plan-changed", _reask_because_the_plan_changed,
         "the plan is no longer the one that was approved — ask again"),
]

# --------------------------------------------------------------------------- wiring

STATE = Path(__file__).with_name(".recovery-case-id")


def build() -> CaseLoop:
    store = PostgresStore(env_file("NEON_MASTER_URL"), schema="abeyance")
    approval = ApprovalLoop(LOOP, store=store,
                            transport=GmailTransport(token_path=GMAIL_TOKEN, sender=SENDER),
                            policy=SINGLE_APPROVER,
                            on_escalate=lambda e: print(f"  ESCALATION[{e.kind.value}] "
                                                        f"{e.detail[:150]}"))
    return CaseLoop(LOOP, store=store, registry=REGISTRY, rules=RULES,
                    policy=CasePolicy(lease_grace_seconds=120, max_attempts=2,
                                      authorization_ttl_seconds=86_400),
                    runner=FlyMachinesRunner(app=APP_DATA, region="iad"),
                    approval=approval, env_for=grants,
                    on_escalate=lambda e: print(f"  ESCALATION[{e.kind.value}] {e.detail[:150]}"))


def current() -> str:
    if not STATE.exists():
        raise SystemExit("no case yet — run `open` first")
    return STATE.read_text().strip()


def show_tick(r) -> None:
    print(f"  status      : {r.status.value}")
    if r.derivation and r.derivation.fired:
        for req in r.derivation.new_requests:
            print(f"  WARRANTED   : {req.need}  by={req.warranted_by}")
            print(f"                because: {req.spec.get('because', req.spec.get('plan','-'))}")
    for rec in (r.dispatch.records if r.dispatch else []):
        print(f"  {rec.action:<12}: {rec.request_id}" + (f"  {rec.detail}" if rec.detail else ""))
    if r.authority:
        print(f"  authorized  : {r.authority.granted}  ({r.authority.reason[:150]})")
        if r.authority.stale_decisions:
            print(f"  STALE YES   : {r.authority.stale_decisions}")
        if r.authority.ignored_claims:
            print(f"  IGNORED     : {r.authority.ignored_claims}")


def contributions_table(cases, case_id) -> None:
    print("\ncontributions:")
    for c in cases.contributions(case_id):
        mark = "  (superseded)" if any(
            o.supersedes == c.id for o in cases.contributions(case_id)) else ""
        print(f"  [{c.kind.value:<14}] {c.actor.id:<28}{mark}")
        print(f"      {c.summary[:112]}")


# --------------------------------------------------------------------------- commands


def cmd_provision(a) -> None:
    r = FlyMachinesRunner(app=APP_DATA, region="iad")
    for app in (APP_DATA, APP_MODEL):
        print(f"  {app:<26} {'created' if r.ensure_app(app, org=a.org) else 'exists'}")
    print("\ncapabilities (the reach ceiling for this case):")
    for c in REGISTRY.all():
        print(f"  {c.name:<24} {c.image:<22} app={c.app:<22} emits={c.emits.value}")
    print("\nrules (what the case may derive):")
    for rule in RULES:
        print(f"  {rule.name:<28} {rule.description}")


def cmd_open(a) -> None:
    cases = build()
    case = cases.open(action="launch-campaign", subject_key=a.client.lower(),
                      title=f"Re-engagement wave for {a.client}",
                      needs=[Need("campaign-performance", spec={"client": a.client})],
                      context={"client": a.client})
    STATE.write_text(case.id)
    print(f"case {case.id}\n  needs: {[r.need for r in case.requests]}")
    print("\ndispatching:")
    show_tick(cases.tick(case.id)[0])


def cmd_tick(a) -> None:
    cases = build()
    cid = current()
    print(f"tick {cid} at {time.strftime('%H:%M:%S')}")
    show_tick(cases.tick(cid, harvest_standing=STANDING)[0])
    contributions_table(cases, cid)


def cmd_recheck(a) -> None:
    """Run the sharper deliverability check, superseding the coarse reading.

    In production this is a monitor on its own schedule, not a command. It is a command here so
    the timeline can be compressed — the machinery is identical either way.
    """
    cases = build()
    cid = current()
    case = cases.get(cid)
    coarse = next((c for c in cases.contributions(cid)
                   if c.request_id == "deliverability-check"), None)
    if coarse is None:
        raise SystemExit("no coarse deliverability reading yet — tick until there is one")

    cap = REGISTRY.get("deliverability-coarse")
    sharp = Capability(
        name="deliverability-sharp", image=cap.image, produces=("deliverability-recheck",),
        emits=ContributionKind.EVIDENCE, reach=cap.reach, app=cap.app,
        entrypoint=cap.entrypoint, cmd=cap.cmd,
        env={**cap.env, "WORKER_B64": b64(SHARP_DELIVERABILITY_SQL)},
        guest=dict(cap.guest), timeout_seconds=120)

    env = {**sharp.env, **grants(sharp, case, case.request("deliverability-check")),
           "ABEYANCE_CASE_ID": case.id,
           "ABEYANCE_REQUEST_ID": "deliverability-check",
           "ABEYANCE_ACTOR": "worker:deliverability-sharp",
           "ABEYANCE_SPEC": json.dumps({"client": case.context.get("client")}),
           "ABEYANCE_CONTRIBUTION_KIND": cases.contribution_kind,
           # A distinct key, so the reading the human was shown survives in the record.
           "ABEYANCE_CONTRIBUTION_KEY": f"{case.id}::deliverability-check::sharp",
           "SUPERSEDE_ID": coarse.id}

    runner = FlyMachinesRunner(app=APP_DATA, region="iad")
    ref = runner.start(image=sharp.image, cmd=list(sharp.cmd), entrypoint=list(sharp.entrypoint),
                       env=env, app=sharp.app, label=f"recheck-{case.id}"[:58],
                       guest=dict(sharp.guest), timeout_seconds=120)
    print(f"sharper re-check dispatched: {ref}")
    print(f"  it will supersede {coarse.id}")
    print("  (in production this is a monitor on its own schedule, not a command)")


def cmd_ask(a) -> None:
    cases = build()
    cid = current()
    case = cases.get(cid)
    contributions = cases.contributions(cid)
    revised = a.revised

    if revised:
        design = next((c for c in contributions if c.request_id == "campaign-design"), None)
        seg = next((c for c in contributions if c.request_id == "segment-analysis"), None)
        sharp = next((c for c in contributions
                      if c.request_id == "deliverability-check"
                      and c.provenance.get("method") == "per-campaign"), None)
        if design is None:
            raise SystemExit("no revised plan yet — tick until campaign-design lands")
        lines = [
            f"Case {case.id} — the plan you approved is no longer safe to run.", "",
            "What changed:",
            f"  {sharp.summary if sharp else '(re-check pending)'}",
            "", "Your earlier approval has been set aside automatically, because the evidence it",
            "rested on has been superseded. Nothing was sent.", "",
            "What the case found instead:",
            f"  {seg.summary if seg else '(no segment analysis)'}",
            "", "Proposed replacement:", f"  {design.payload.get('plan')}",
            "", "Note: the designer's own payload claims to authorize this. It cannot — it is a",
            "recommendation. Only your reply carries authority.", "",
            "Reply 'approve 1' to authorize the revised plan, or 'reject 1' to stop.",
        ]
        summary = (f"REVISED: {design.payload.get('plan')[:90]}")
        request_id = "revised-decision"
    else:
        lines = [f"Case {case.id}", f"Client: {case.context.get('client')}", "",
                 "What the workers found:"]
        for c in contributions:
            lines.append(f"  [{c.kind.value}] {c.summary}")
        lines += ["", "Reply 'approve 1' to authorize, or 'reject 1' to decline."]
        summary = f"Launch a re-engagement wave for {case.context.get('client')}"
        request_id = "human-decision"

    res = cases.ask_humans(cid, summary=summary, detail="\n".join(lines),
                           request_id=request_id,
                           approvers=[Approver(APPROVER, role="owner")],
                           dry_run=a.dry_run)
    if a.dry_run:
        print(res.body)
        return
    print(f"asked {APPROVER} about {request_id!r} — thread {res.id}")


def cmd_decide(a) -> None:
    cases = build()
    cid = current()
    loop = cases.approval
    case = cases.get(cid)
    inbound = loop.read(case.proposal_id)
    if not inbound:
        print("no unconsumed reply on the thread")
        return
    for i in inbound:
        print(f"reply from {i.sender}: {i.text[:200]!r}")
        print(f"  parser: approve={i.suggestion.approve} confident={i.suggestion.confident}")
    loop.record(case.proposal_id, inbound[-1].sender,
                approve=[1] if a.decision == "approve" else [],
                reject=[1] if a.decision == "reject" else [],
                raw=inbound[-1].text,
                reply_ids=[i.reply.message_id for i in inbound])
    print(f"\nrecorded: {a.decision}\n")
    show_tick(cases.tick(cid, harvest_standing=STANDING)[0])


def cmd_execute(a) -> None:
    cases = build()
    cid = current()

    def executor(case, auth, contributions):
        body = [f"Case {case.id} — {case.title}", "",
                f"Authorized by : {', '.join(auth.granted_by)}",
                f"Scope         : {json.dumps(auth.scope)}", "",
                "Resting on:"]
        for c in contributions:
            if c.id in auth.basis:
                body.append(f"  [{c.kind.value}] {c.summary[:96]}")
        body += ["", "Nothing was actually sent — this is a mechanism test."]
        cases.approval.transport.send(to=RECEIPT_TO,
                                      subject=f"[abeyance] executed — {case.title}",
                                      body="\n".join(body))
        return {"launched": False, "scope": auth.scope}

    out = cases.execute(cid, executor, dry_run=a.dry_run)
    print(json.dumps(out.to_doc(), indent=2, default=str)[:1800])


def cmd_show(a) -> None:
    cases = build()
    cid = current()
    case = cases.get(cid)
    print(f"case   : {case.id}\ntitle  : {case.title}\nstatus : {case.status.value}")
    print("\nrequests:")
    for r in case.requests:
        print(f"  {r.id:<22} {r.status.value:<11} cap={r.capability or '(out of band)':<22} "
              f"by={r.warranted_by}")
    contributions_table(cases, cid)
    auth = cases.authority(cid)
    print(f"\nauthority: {auth.granted} — {auth.reason[:160]}")
    if auth.authorization:
        print(f"  scope  : {auth.authorization.scope}")
        print(f"  by     : {auth.authorization.granted_by}")
    print("\nhistory:")
    for h in case.history:
        stamp = time.strftime("%H:%M", time.localtime(h.get("at", 0)))
        rest = {k: v for k, v in h.items() if k not in ("event", "at")}
        print(f"  {stamp}  {h['event']:<20} {json.dumps(rest, default=str)[:110]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("provision"); p.add_argument("--org", default="kai-karlstrom")
    p.set_defaults(fn=cmd_provision)
    p = sub.add_parser("open"); p.add_argument("--client", default="Northwind")
    p.set_defaults(fn=cmd_open)
    sub.add_parser("tick").set_defaults(fn=cmd_tick)
    sub.add_parser("recheck").set_defaults(fn=cmd_recheck)
    p = sub.add_parser("ask")
    p.add_argument("--revised", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("ask-revised")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_ask, revised=True)
    p = sub.add_parser("decide")
    p.add_argument("decision", choices=["approve", "reject"])
    p.add_argument("--revised", action="store_true")
    p.set_defaults(fn=cmd_decide)
    p = sub.add_parser("execute"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_execute)
    sub.add_parser("show").set_defaults(fn=cmd_show)
    args = ap.parse_args()
    if not hasattr(args, "revised"):
        args.revised = False
    args.fn(args)


if __name__ == "__main__":
    main()
