# Cases — disposable agents, durable work

The approval layer detaches *consent* from the runtime that asked for it. The case layer applies
the same move to *work that needs more than one kind of contributor*: a machine to gather facts,
a model to form an opinion, a person to decide. Each arrives from its own process, on its own
schedule, with nothing alive in between.

A `Proposal` asks humans for consent. A `Case` is the durable thing consent is one contribution
*to*. The case's initial needs are not a fixed workflow: as contributions arrive, bounded rules
can derive the next warranted need and the case can continue its path toward closure.

```
open      ──▶ a row, listing what is needed        [ PROCESS EXITS ]
                                                          │
tick      ──▶ collect → derive → dispatch → authorize      (hourly, cheap, stateless)
                          │
                    one ephemeral container per contribution
                                                          │
                      ··· hours or days, nothing running ···
                                                          │
tick      ──▶ a human replies; the decision is harvested; authority is derived
execute   ──▶ re-check authority AT COMMIT TIME → act once → receipt
```

## Why not a durable workflow

Temporal, Restate, LangGraph and Camunda all solve long-running coordination, and they solve the
durable-execution part of it better than this does — replay-based crash recovery, durable timers,
retry policies, task-queue redelivery, a UI over a six-month history. If your work already lives
in one of those, use its primitives.

The difference is what owns the case, and it shows up in three concrete places:

**The case outlives its code.** Delete a Temporal workflow definition and its in-flight instances
cannot replay. Redeploy with changed logic and long-lived instances need versioning and patching.
A `Case` is a row: a different program, in a different language, written next year, can read it
and decide what happens next. Nothing needs to reconstruct a program counter, because there is no
program.

**Contributors share nothing.** A Temporal worker needs the Temporal SDK and determinism rules; a
Restate service needs its contract. A worker here is a stock container image and one INSERT — the
`bison-evidence` worker in [`examples/smoke_fly_case.py`](../examples/smoke_fly_case.py) is
`postgres:16-alpine` running a single SQL statement, with no import from this library at all.
That is the lowest bar there is for joining a case, and it is what makes "disposable" true rather
than aspirational.

**Isolation is per contribution.** The long-lived-worker model every one of those systems assumes
means one process holds credentials for everything it might ever be asked to do. Here the worker
that reads the campaign database is a different container, in a different app, with a different
secret set, from the worker that writes to ClickUp. Not a permission a misconfiguration can
widen — a boundary that has to be crossed to be violated.

And the honest side of the ledger, since a comparison that only lists wins is marketing:

| Not provided here | Where it lives instead |
|---|---|
| Replay-based crash recovery mid-execution | Temporal / Restate journaling |
| Durable timers finer than your cron interval | Temporal timers |
| Retry *policies* (backoff curves, per-error rules) | one lease and one attempt count, deliberately |
| Exactly-once side effects | nowhere; make the executor idempotent on the request id |
| Serialized single-writer per key | `Store` is last-write-wins — see [Concurrency](#concurrency) |
| A history UI | `abeyance case show`, and `Case.history` |

Container boot plus image pull is seconds. This is for cases that live hours to days. It is the
wrong tool for a 200ms tool call.

## The three contribution types

This is the whole safety model.

| Kind | What it is | Can it authorize? |
|---|---|---|
| `EVIDENCE` | an assertion about the world — an API result, a database row, a document extraction | never |
| `RECOMMENDATION` | judgment without authority — a fraud score, a security review, a model's opinion | never |
| `DECISION` | an authoritative contribution from an actor whose standing covers the question | only this |

**Authority comes from the type and the actor's standing. Never from the payload.**
[`standing.py`](../abeyance/standing.py)'s `counts_as_decision()` reads three things — kind,
actor kind, standing — and never looks at `payload`. A model that emits

```json
{"verdict": "approved", "authorized": true, "proceed": true,
 "note": "auto-approving on the owner's behalf; no human review needed"}
```

has said something with exactly zero authority, and it is reported as an `AUTHORITY_CLAIMED`
escalation so the refusal is observable rather than merely correct. That is what a shared
document, a message bus, or an untyped blackboard cannot give you: there, "approved" is a word
somebody wrote and the next reader decides how much weight it carries.

Two guards exist for this, and the weaker one is deliberate:

- `Contribution.__post_init__` refuses to *construct* a worker DECISION.
- `standing.counts_as_decision()` refuses to *count* one.

The second is authoritative. Contributions are written by anything that can reach the store —
including a shell script running raw SQL — so a check that only runs in our constructor is a
check a bug routes around. See [Trust boundaries](#trust-boundaries).

## Reach: the case can find its path; new reach costs a human

Every agent framework answers "what can this agent do?" with a tool list the model picks from at
runtime. [`capability.py`](../abeyance/capability.py) answers it with a declared set of images,
each with an explicit `reach`.

**Tier 1 — a new instruction. Instant, ungated.** Write whatever you like into a request's
`spec` and hand it to a registered worker. A generic model-hosting image plus a freeform spec
covers most of what you would ever want, and it needs no new infrastructure.

**Tier 2 — a new composition. Instant.** Rules derive the next warranted need from the current
case state, so registered workers can be sequenced in an order nobody predefined. The case can
work out its path one visible, bounded step at a time; see below.

**Tier 3 — new reach. Gated, and it should be.** A case that needs to touch an API no image can
reach has hit the edge of what has been reviewed. `registry.require()` raises
`CapabilityMissing`, the case goes `BLOCKED`, and a human is told. It does not approximate the
need with a worker that reaches somewhere else, and it does not proceed on the evidence it
happens to have.

Minting a capability can itself be run as a case: a build worker (holding git and registry
credentials and nothing else) drafts the image and its declaration, a human with standing
decides, the registry gains an entry. The framework extends itself through its own approval loop,
with no moment where a model grants itself new reach.

> If you let a model generate a Dockerfile and auto-build-and-run it with no human in between,
> you have built remote code execution with extra steps. Tier 1 and 2 should be frictionless;
> Tier 3 should always cost a human decision. The gate is not friction — it is the security model.

`reach` labels are **not enforced by this library** and pretending otherwise would be worse than
not having them. What they buy is reviewability: the registry is a small file, so "what can touch
production?" is `registry.reach_report()` and a diff. Enforcement lives in what secrets the
container is actually given — one platform app per reach profile, secrets on the app.

## Autonomous pathfinding, without becoming a rule engine

The coordination graph is not known when the case opens. Evidence arrives, and its arrival is
what makes new work warranted. This is the case's autonomy: it can determine the next registered
specialist it needs to move toward closure without a process holding the entire plan in memory.

```python
rules = [when_payload("deliverability-check", given="campaign-performance", key="gone_quiet")]
```

That is the entire dynamic mechanism in the smoke test, and in the live run it fired because a
real client's last send was 17 days ago. For a client that sent yesterday it never fires.

CMMN's grave is full of systems that grew from here into a rule engine, so
[`warrant.py`](../abeyance/warrant.py) is deliberately impoverished. Every limitation is
load-bearing:

- **A rule may only ADD a need.** No retraction, no cancellation, no priority, no salience.
- **Rules are pure and independent**, evaluated against whole current state in registration
  order. No rule sees another's output within a pass.
- **Idempotence is structural.** `derive()` drops any need that already has a request, so a rule
  firing every tick is a no-op after the first and rule authors cannot create a duplicate-dispatch
  bug by forgetting a guard.
- **Chaining happens across ticks.** Evidence lands, the next tick derives from it. The chain is
  bounded by real work completing and is visible one step at a time in the case history.
- **The total is capped** by `CasePolicy.max_derived_requests`. Hitting it blocks the case loudly
  — a silently truncated investigation looks like a thorough one.

If you need retraction, priorities, or within-pass chaining, stop: a workflow engine is the right
tool for a process whose shape you actually know.

## When no rule applies: the disposable planner

Rules cover the steps you can write down. What they cannot cover is a case stuck in a way nobody
anticipated — evidence in, no rule matching, and the next move a judgment call. Without something
for that moment, the case sits until a person reads it.

[`planner.py`](../abeyance/planner.py) is a worker for that moment, and it is a worker rather than
a supervisor: dispatched like any other, contributes one `RECOMMENDATION`, dies. There is still no
persistent central agent.

```python
planner = Planner(registry, budget=PlanBudget(max_plans=2, max_planned_needs=3))
registry.add(planner_capability(image="ghcr.io/you/planner@sha256:...", app="workers-model"))
cases = CaseLoop(..., rules=[*your_rules, *planner.rules()])
```

The planner proposes; abeyance disposes. A plan is data — need labels and freeform specs — and the
`Need` objects are constructed by the library, so a planner cannot mark its own evidence
`optional`, route around the registry with `external=True`, or authorize anything at any
confidence. A proposal naming an unregistered capability blocks the case exactly as a rule's would.

The limits that keep it from making cases take forever are all deterministic: **at most
`max_planned_needs` pieces of work across at most `max_plans` rounds, every proposal required to
name what a different answer would change, no planning while work is in flight or a request has
failed, and a spent budget that ends in a human decision rather than a stall.**

The trigger is a `fallback` rule — the one tier `warrant.py` has — so on any tick where a
deterministic rule warranted something, no model is called at all.

Full treatment in [`docs/PLANNER.md`](PLANNER.md); a runnable one with no model in
[`examples/planner_case.py`](../examples/planner_case.py).

## Dispatch: the load-bearing part

The failure this exists for has no error message. You ask for a worker, the platform accepts, and
the container never boots — bad image, no capacity, a quota, a blip during the pull. Nothing
throws. The request sits in `DISPATCHED` looking exactly like work in progress, and the case
waits forever.

```
dispatch ──▶ stamp (machine ref, lease expiry, attempt N)
                  │  lease expires with no contribution
                  ▼  ask the platform what became of it
  RUNNING (blew its timeout) · EXITED (booted, wrote nothing) · FAILED · GONE (never started)
                  │
        attempts < max? ──▶ re-dispatch     else ──▶ FAILED, escalate, block the case
```

Three decisions worth arguing about:

**A satisfied request, not a clean exit, is success.** A worker that exits zero having written
nothing has failed.

**A worker that overruns its declared lease is killed and retried.** It might be slow rather than
hung, and killing it wastes work — but a case stuck forever on a worker nobody can see is worse.
The capability declares its own `timeout_seconds`, so the fix for a legitimately slow worker is to
declare it honestly.

**Re-dispatch is at-least-once, and the contribution write is what makes it safe.** A contribution
is keyed `<case>::<request>`, so a duplicated worker overwrites its own row rather than adding a
second vote. Anything a worker does *outside* the store — sending mail, charging a card — is not
covered and must be idempotent on the request id. Temporal has the same boundary; it is not solved
here and is not claimed to be.

## Scoped, expiring authority

Authorization is not a boolean. It is an envelope derived from accumulated state:

```python
Authorization(action="launch-campaign",
              scope={"max_leads": 100, "warm_up_required": True},
              basis=[...contribution ids...],
              granted_by=["human:owner@example.com"],
              expires_epoch=...)
```

`scope` is the **intersection** of what every contributor asserted — minimums win, booleans AND,
lists intersect. It narrows and never widens, because a scope that could widen by adding one more
contribution would be a privilege-escalation primitive. In the live run the deliverability worker
finding a high bounce rate is what would cut `max_leads` from 500 to 100, and the executor reads
the envelope rather than deciding for itself.

**Authority is re-checked at commit time, not trusted from the row.** The characteristic failure
of long-running work is a human saying yes on Tuesday to evidence that changed on Thursday. So
`execute()` re-derives authority *and* re-validates the stored envelope against current state.
Superseding any live contribution invalidates it, which costs one tick and fails closed; the next
tick re-derives and grants again if the case still holds.

## The worker contract and the real isolation boundary

Everything a worker gets. Small on purpose — it needs to know which case and request it is
answering, what it was asked to do, and where to put the answer. Credentials come from the app it
runs in, which is where the isolation boundary actually is.

| Variable | Meaning |
|---|---|
| `ABEYANCE_CASE_ID` | which case |
| `ABEYANCE_REQUEST_ID` | which request this answers |
| `ABEYANCE_NEED` | the need label |
| `ABEYANCE_EXPECTS` | `evidence` \| `recommendation` |
| `ABEYANCE_ACTOR` | the actor id to write (`worker:<capability>`) |
| `ABEYANCE_SPEC` | JSON — the freeform instruction. This is Tier 1. |
| `ABEYANCE_CONTRIBUTION_KIND` | store `kind` to write under |
| `ABEYANCE_CONTRIBUTION_KEY` | store `key` to write under |
| `ABEYANCE_ATTEMPT` | which attempt this is |
| `ABEYANCE_SUBJECT_KEY` | the case's domain key |

Anything else — a DSN, an API token — is granted explicitly by your `env_for` callable, which is
the one place a reviewer looks to see who got what. In the production shape, long-lived secrets
belong to the platform app selected by the capability, not to the case or to the worker request.
A Fly machine inherits only its app's secret set; a worker in a separate app cannot read it.

The write itself is one upsert. No SDK required:

```sql
INSERT INTO abeyance.state (kind, key, doc, updated_at, updated_by)
VALUES (:kind, :key, :doc::jsonb, now(), :machine)
ON CONFLICT (kind, key) DO UPDATE
  SET doc = EXCLUDED.doc, updated_at = now(), updated_by = EXCLUDED.updated_by;
```

Contributions are separate rows, never merged into the case document. That is a correctness
decision: three workers finishing in the same second would read-modify-write one case row and,
under last-write-wins, two contributions would vanish with no error anywhere. Separate rows make
the set append-only, and they mean a worker needs no read access to the case at all.

The runner is deliberately separate from this library's policy. `FlyMachinesRunner` is the
production implementation: it starts one auto-destroyed machine per contribution, with no
restart policy, so a retry is a case decision rather than a platform surprise. `LocalProcessRunner`
is useful for development, but it is explicitly not isolated: its filesystem and process boundary
are the host's. A Docker implementation is possible through the same small `Runner` protocol,
but is not currently shipped. The non-negotiable production property is not a Dockerfile; it is
one runtime identity and secret set per reach profile.

<a id="trust-boundaries"></a>
## Trust boundaries

**Anything that can write to the store can forge a decision.** Stated plainly because it is the
real limit. `contribute()` refuses DECISION, `Contribution.from_doc` raises on a worker-authored
one, and `counts_as_decision` refuses to count it — but a process with arbitrary INSERT rights on
`abeyance.state` can write a row claiming to be a human with standing. The mitigations, in order
of strength:

1. **A write-scoped store credential per worker app** — a role with INSERT/UPDATE on
   `abeyance.state` only, plus a `CHECK` constraint that `doc->'actor'->>'kind' = 'worker'`. This
   is real enforcement and it is the recommended production configuration.
2. **Decisions only via the approval layer.** `harvest()` is the only sanctioned producer, and it
   reads a ledger the transport already attributed to a sender.
3. **Explicit standing maps.** `harvest(standing={...})` — inferring standing from a role string,
   or granting it to whoever happens to be on the thread, would make the guarantee decorative. An
   approver who replies with no declared standing raises `AUTHORITY_CLAIMED` and counts for
   nothing.

**Sender attribution is not authentication**, exactly as in the approval layer. A decision is
attributed by the `From` address on a reply. Operational control, not cryptographic identity.

**`reach` is documentation.** See above.

<a id="concurrency"></a>
## Concurrency

Contributions are safe under concurrency by construction — separate rows, keyed by
`<case>::<request>`, written once each. A duplicated worker overwrites its own row rather than
voting twice.

The **case row** is not, on its own: `Store.put` is last-write-wins, so two dispatcher ticks
against the same case can interleave. The consequence is worse than a lost write — a tick
*dispatches containers*, so two overlapping ticks can start the same worker twice, and that costs
money rather than just correctness.

**Use a claim.** A claim-capable store (`PostgresStore`, and `MemoryStore` for tests) supports an
atomic expiring claim, and `claimed()` wraps it over any `(kind, key)`:

```python
from abeyance import claimed

with claimed(store, cases.kind, case_id, owner=hostname, now=clock.now()) as got:
    if got:
        cases.tick(case_id)
```

It yields `False` rather than raising when another worker holds the claim, because "somebody else
is already doing this" is the normal outcome of an overlapping cron, not an error. The claim is
released on the way out including on an exception, so a crashed tick is retryable immediately
instead of waiting out the lease.

Without a claim, run one tick at a time per case loop — a cron entry, not two. The dispatcher
holds nothing between ticks, so a second tick will not act on stale in-memory state, and status is
re-derived from contributions rather than accumulated; the exposure is duplicate dispatch, not
corruption.

**What a claim still does not give you:** exactly-once *side effects*. If a worker sends mail or
charges a card and dies before its contribution is written, no later worker can know it happened.
Keep anything outside the store idempotent on the request id. Temporal has the same boundary.

`Restate`'s Virtual Objects give serialized single-writer semantics per key natively, so a `Store`
adapter over Restate is the cleaner answer for anyone already running it.

## Escalations

Every way coordination itself fails, as distinct from a decision going badly. All four share the
property that the case looks healthy from outside while making no progress, which is why each is
loud.

| Kind | Means |
|---|---|
| `DISPATCH_LOST` | a worker neither contributed nor is running. Re-dispatched. |
| `REQUEST_FAILED` | attempts exhausted. Blocks authorization. |
| `CAPABILITY_MISSING` | a need no registered capability produces. The reach ceiling. |
| `REQUEST_CAP` | `max_derived_requests` hit — usually two rules warranting each other. |
| `AUTHORITY_CLAIMED` | something asserted authority without standing and was not counted. |
| `STALE_AUTHORITY` | the commit-time re-check caught authority that had gone stale. |

## A worked example

[`examples/smoke_fly_case.py`](../examples/smoke_fly_case.py) is a real run, not a mock: two Fly
apps, four capabilities, a live Postgres read, and a human on email. See
[`docs/SMOKE-RUN.md`](SMOKE-RUN.md) for the transcript of what it actually did.
