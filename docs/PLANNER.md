# The planner — a disposable agent that decides what to try next

`warrant.py` covers the steps you can write down. A rule fires because a number crossed a line, and
that covers most of real coordination. It cannot cover the moment a case is stuck *in a way nobody
anticipated*: the evidence is in, no rule matches it, and the honest next move is a judgment call.

Without a planner, that moment ends the case's autonomy. The case sits until a person reads it.

The planner is a worker for exactly that moment — and it is a worker, not a supervisor. It is
dispatched like any other, contributes one typed thing, and dies. There is still no persistent
central agent, and the case is still the only durable thing.

```
stuck case ──▶ tick ──▶ one planner worker ──▶ a RECOMMENDATION ──▶ tick validates it
                              │                                            │
                             dies                              registered? ──▶ dispatch worker
                                                                        no ──▶ BLOCKED, ask a human
```

## The separation

**The planner decides what should be tried. Abeyance decides what is allowed.**

A plan is a `RECOMMENDATION` — the same contribution kind a fraud score or a security review
writes, carrying the same authority, which is none. It never reaches
[`standing.py`](../abeyance/standing.py). What it produces is *data*: need labels, one-line
reasons, and freeform specs.

| The planner may | The planner may not |
|---|---|
| choose among registered capabilities | create a credential or widen reach |
| compose them in an order nobody predefined | authorize anything, at any confidence |
| write a task-specific `spec` for any of them | answer a human's question, or stand in for one |
| say more investigation is warranted | mark its own evidence `optional` or `external` |
| say the case is ready for a person to decide | grant itself another planning round |

The last two rows are structural rather than policed. `PlanProposal` has four fields — `need`,
`why`, `changes_decision_if`, `spec` — and no `optional` or `external`, because the `Need` is
constructed by the library from that data. And a proposal naming the planner's own need is
rejected by name: the one proposal a planner has an obvious incentive to make is the one it cannot
make.

## The part that matters: it cannot make a case take forever

This is the real risk, and it is not a safety risk — it is that the planner is *useful enough to
keep going*. An agent asked "what else should we look at?" will always have an answer. Each round
is individually reasonable. The case never closes.

Six limits prevent that, and **every one is deterministic**. None of them depends on the model
being disciplined; they are checked against the case's own rows, by code the planner does not run.

| Limit | Default | What it stops |
|---|---|---|
| `max_plans` | 2 | Planning rounds per case, ever. Not per day, not per phase. |
| `max_needs_per_plan` | 2 | A plan that names five things has not prioritised. |
| `max_planned_needs` | 3 | Total work a planner may add across every round. |
| `require_decision_relevance` | on | Every proposal must name what a different answer would change. |
| Standstill guards | — | No planning while work is in flight, a human is deciding, or a request has **failed**. |
| `ask_human_when_spent` | on | A spent budget ends in a decision, not a stall. |

The sentence to hold onto: **a planner can add at most `max_planned_needs` pieces of work to a
case, across at most `max_plans` rounds, and then the case goes to a person.**

Three of those deserve their reasoning.

**`changes_decision_if` is the cheapest filter that exists.** Every proposal must state the finding
that would change what the case does — "if any single campaign is over 3% we cut the wave to 100
and warm up first". A proposal that leaves it blank is dropped before it costs a container. The
question has no good answer for the investigation nobody needs, and a planner cannot bluff past a
string-emptiness check. This one line removes most of the "one more thing" failure on its own.

**Never plan around a failed request.** The only guard here that is about correctness rather than
cost. A `FAILED` request is a hole in the record; planning past it is precisely "we could not
gather it, so we proceeded on what we had", which is the failure the case layer exists to refuse.
A person fixes the worker, cancels the request, or marks it optional.

**A round that yields nothing usable goes straight to a person.** No retry, no second attempt at
the same standstill. The planner had its turn, and a planner that gets another turn for having
been unhelpful is a planner with an unbounded budget.

One asymmetry is deliberate: the planner's own request is `optional`. A planner image that will not
boot is escalated as `REQUEST_FAILED` like any other worker — loudly — but it does not block the
case. Everything needed to put the question in front of a person is already on the record, and
holding a decidable case hostage because the *advisor* crashed is the opposite of the point. The
round still counts against the budget, so a failing planner cannot buy itself extra attempts.

## Wiring

```python
from abeyance import CaseLoop, Planner, PlanBudget, planner_capability

planner = Planner(registry, budget=PlanBudget(max_plans=2, max_planned_needs=3))
registry.add(planner_capability(image="ghcr.io/you/planner@sha256:...", app="workers-model"))

cases = CaseLoop("launches", store=store, registry=registry, runner=runner,
                 approval=approval_loop,
                 rules=[*your_deterministic_rules, *planner.rules()])
```

`planner.rules()` returns two:

- **`planner:adopt`** — an ordinary rule. A plan has landed; validate it and turn what survives
  into requests.
- **`planner:trigger`** — a **fallback** rule. It runs only on a tick where no ordinary rule
  warranted anything.

That second word is the whole integration. `Rule.fallback` is the one tier
[`warrant.py`](../abeyance/warrant.py) has, and it exists because "here is what to do" and
"nothing applies, now what" are different questions. Deterministic work always wins; on any tick
where a rule you wrote had something to say, no model is called at all.

## The worker

Not shipped, deliberately — this library holds no model router and no API key. What *is* shipped
is the entire instruction, assembled into the request's `spec`:

```jsonc
{
  "role": "planner",
  "instructions": "...",              // PLANNER_INSTRUCTIONS — the pragmatism, in words
  "output_schema": { ... },           // PLAN_SCHEMA
  "budget": {"rounds_left": 1, "needs_you_may_propose": 2, ...},
  "case": {
    "goal": "...", "action": "launch-campaign", "status": "open",
    "requests": [...],                // what has been asked, and where each stands
    "evidence": [...],                // bounded; anything omitted is marked, never silently cut
    "recommendations": [...],
    "decisions": [...],
    "previous_plans": [{"proposed": [...], "adopted": [...], "not_adopted": [...]}]
  },
  "capabilities": [{"need": "...", "emits": "evidence", "description": "..."}]
}
```

So the worker is a generic model-hosting image: read `ABEYANCE_SPEC`, call a model, write one
contribution. [`examples/planner_case.py`](../examples/planner_case.py) is a working one, in about
thirty lines and with no model at all.

Three things about that brief are deliberate:

**No images, apps, reach labels or credentials.** A planner picks among questions it can have
answered. How an answer gets fetched, and what credentials that takes, is not its business.

**`previous_plans` carries `not_adopted`.** The convergence feedback loop, and the cheapest one
available: a planner that can see its predecessor proposed `vendor-review` and that no request
came of it will not spend a slot proposing it again. Both halves are read from durable state.

**Truncation is marked.** A planner reasoning over "all the evidence" when it was shown two thirds
of it is exactly the quiet wrongness this library is shaped against, so an omitted contribution
becomes a `_dropped` count and an oversized payload becomes `{"_truncated": true, ...}`.

## The reach ceiling, from the planner's side

If a plan names a need no registered capability produces — whether in `missing_capabilities` or as
a proposal — the case goes `BLOCKED` and `CAPABILITY_MISSING` names what is missing. Nothing else
in that plan is adopted: half-executing a plan whose own author said it was incomplete costs a
round to find that out, and blocking is the answer the case layer already has for this.

The resume path needs no operator action beyond minting the worker. The adopter re-emits the same
need on every tick, so the moment the capability is in the registry it matches, becomes a request,
and the case carries on from exactly where it stopped.

The escalation fires **once per set of missing needs**, not once per tick, and a case blocked this
way still expires on schedule — a capability gap is a standing condition, and an alert that repeats
hourly for a week is an alert somebody filters.

## Watching it

```bash
abeyance --app app:cases case-plan --id <case-id>
```

```jsonc
{
  "rounds_used": 1, "rounds_left": 1,
  "needs_added": 1, "needs_left": 2,
  "would_plan_now": false,
  "why_not": "work is in flight: ['deliverability-check']",
  "review": {
    "accepted": [{"need": "deliverability-check", ...}],
    "rejected": [{"need": "fit-score", "reason": "no-changes_decision_if"}],
    "missing": [], "ready": false
  }
}
```

`why_not` is there because "why didn't the planner run?" is the first question anybody asks of a
case that sat still, and it should be answerable without attaching a debugger to a cron job.

The review is never written down — it is a pure function of the plan, the case and the registry —
so running this a year later prints what the tick that acted on it saw. Every request a plan
produced also carries `_plan` in its spec, holding that plan's contribution id, so "why is there a
deliverability check on this case?" resolves to a specific plan, its rationale, and its round.

## When not to use one

- **Your next step is knowable.** Write the rule. It is cheaper, faster, testable, and it does not
  need a budget.
- **You want the case to be thorough.** A planner is tuned for closure. `max_planned_needs=3` is a
  ceiling on curiosity, not a target.
- **The evidence is missing rather than ambiguous.** A planner will not plan around a failed
  request, which is correct, so what you need is a person fixing the worker.
- **You want it to decide.** It cannot, at any accuracy. Pair it with an
  [`ApprovalLoop`](../abeyance/loop.py); the planner's best outcome is putting a well-formed
  question in front of somebody who can answer it.
