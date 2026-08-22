# Changelog

## 0.3.0 — unreleased

**The disposable planner (`planner.py`).** An optional worker for the moment no rule applies: the
evidence is in, nothing matches it, and the next move is a judgment call. It reads the case, picks
from the registered capabilities, proposes what to try next, and dies. There is still no persistent
central agent — a plan is a `RECOMMENDATION`, with the authority every recommendation has, which is
none.

The planner decides what should be tried; abeyance decides what is allowed. A plan is *data* — need
labels, one-line reasons, freeform specs — and the `Need` objects are constructed by the library
from that data, which is why a planner cannot mark its own evidence `optional`, route around the
registry with `external=True`, or propose more planning. A need no registered capability produces
blocks the case and names the gap, exactly as a rule's would.

The design risk is not escalation, it is usefulness: an agent asked "what else should we look at?"
always has an answer, and a case that keeps finding one never closes. Six limits, all deterministic
and all checked against the case's own rows:

- `max_plans` (default 2) — planning rounds per case, ever.
- `max_planned_needs` (default 3) — total work a planner may add, and `max_needs_per_plan`
  (default 2) in any one round.
- Every proposal must fill in `changes_decision_if`. A blank one is dropped before it costs a
  container, and the question has no good answer for the investigation nobody needs.
- Standstill guards — never while work is in flight, a human is deciding, or a request has
  **FAILED**. Planning past a hole in the evidence is "we could not gather it, so we proceeded".
- A spent budget warrants the human decision on what is on the record. The terminal state of
  pathfinding is a decision, not another loop.
- A round that proposes nothing usable goes straight to a person. No retry at the same standstill.

Also: `planner_capability()` (the least-privileged worker there is — `model-api` reach and nothing
else), `abeyance case-plan` for what was proposed and what was dropped and why, and
`examples/planner_case.py`, which runs the whole shape with no model, no platform and no key.

**`Rule.fallback`** — the one tier `warrant.py` has. A fallback rule is evaluated only on a pass
where no ordinary rule warranted anything, because "here is what to do" and "nothing applies, now
what" are different questions. It adds no agenda and no priorities; within each tier rules remain
pure, independent and in registration order. This is what makes a planner the last resort rather
than a competitor to the rules you wrote.

**`derive()` keys idempotence on the request id, not the need label.** `Need.request_id` documented
the case of one case legitimately wanting the same kind of work twice; keying on the label made it
silently impossible through rules. Identical behaviour for every rule that does not set it.

**A capability gap is a standing condition, not an hourly alarm.** Nobody mints a worker by waiting,
so an unmatched need re-derives on every tick. `CAPABILITY_MISSING` (and `REQUEST_CAP`) now escalate
once per set rather than once per tick, and the unchanged case is no longer re-saved every tick —
which used to mark it active, so a case blocked on a capability nobody was going to build sat there
looking tended and never expired.

## 0.2.0 — unreleased

**The case layer.** The approval layer detaches consent from the runtime that asked for it; this
applies the same move to work needing more than one kind of contributor. `Proposal` and
`ApprovalLoop` are untouched — a `Case` sits beside them and delegates to an `ApprovalLoop` when
it needs a human, so email threading, reply attribution, deadlock and expiry are reused rather
than reimplemented.

- `capability.py` — the reach ceiling. Which workers exist and what each may touch. New behaviour
  is free (a spec handed to a registered worker); new reach costs a human. A need nothing can
  produce blocks the case rather than being improvised around.
- `standing.py` — the authority math, pure and one screen, sibling of `verdict.py`. Authority
  derives from the contribution's TYPE and the actor's STANDING, never from the payload.
- `warrant.py` — dynamic activity selection, deliberately impoverished: rules may only ADD needs,
  are pure and independent, structurally idempotent, chained across ticks rather than within one,
  and capped. Those five limits are what keep it from becoming a rule engine.
- `dispatch.py` — the lease. A container the platform accepted and never booted throws nothing and
  looks exactly like work in progress.
- `cases.py` — orchestration, plus the commit-time authority re-check.
- `adapters/runners.py` — `MemoryRunner`, `LocalProcessRunner`, `FlyMachinesRunner` (the last over
  stdlib HTTP; a runner that dragged in an SDK would put a dependency in the path of every
  dispatch).
- A fifth seam (`Runner`), `CasePolicy`, six new escalation kinds, eight `case-*` CLI subcommands.
  Core remains dependency-free.

**Contributions are separate store rows**, not nested in the case document. Three workers
finishing in the same second would otherwise read-modify-write one row and lose two of them under
last-write-wins, with no error anywhere. Separate rows make the set append-only and reduce the
worker's contract to a single `INSERT` — which is what lets a worker be a shell script with `psql`
and no SDK at all.

**A decision does not outlive the facts it was given for.** A decision records the evidence it
rested on; superseding that evidence stops it counting, and `execute()` re-derives authority *and*
re-validates the stored envelope at commit time. Decisions are immutable once harvested — a
genuinely new answer re-stamps, a re-harvest of the same reply does not.

**Nine bugs found by three live runs that 244 tests did not catch**, each now pinned by a test and
written up in `docs/SMOKE-RUN.md` rather than quietly fixed. The two that mattered most:
`harvest()` re-stamped decisions it had already recorded, so a stale decision came back to life on
the next tick *and* the record was rewritten to claim the person approved on evidence that did not
yet exist; and stale decisions stayed in the authorization basis, so their dead dependency failed
the commit-time check on every future envelope — a recovered case would report authorized and
refuse to execute, forever.

**Docs.** `docs/CASES.md` (including a table of what is deliberately *not* provided and where it
lives instead) and `docs/SMOKE-RUN.md` (what the live runs did, every bug they found, and an
explicit "what this does not prove").

## Unreleased — 0.1.x

**Positioning corrected.** The original framing claimed every human-in-the-loop library
requires a live process holding the wait. That is false — LangGraph resumes an `interrupt()`
from a checkpointer, Temporal signals durable workflow state, both surviving process death.
The real category is **durable consent detached from the agent runtime**, which makes this
complementary to those systems rather than a competitor to them. README, `__init__`, `loop`,
the architecture doc and the two-approver example rewritten accordingly, plus a table of where
it sits against LangGraph/Temporal, JamJet, AgentGate, HumanLayer ACP and Cloudflare Agents.

**Limits documented rather than implied.** An explicit "what this does not claim": safe for a
*serialized* apply worker and not exactly-once across distributed workers; sender attribution
is an operational control, not authentication; and the exact expiry rule.

**Expiry rule stated precisely and pinned by tests.** `last_activity_epoch` moves on *recorded*
activity — `record`, `dismiss`, `ask`, `confirm`, `execute` — and not on merely fetching an
inbound with `read()`/`poll()`. Deliberate, since resetting on any inbound would let an
out-of-office keep a dead proposal alive forever. Two new tests cover both halves, including
the cost: an ambiguous reply near the deadline needs a deliberate act, which is why
`record_from()` raises `AMBIGUOUS_REPLY` instead of running out the clock.

## 0.1.0 — unreleased

First cut. Extracted from eleven near-identical propose/apply loops running in production
against ~35 client workspaces, plus a two-track, dual-approval loop that pushed the shape past
what a single-approver implementation could carry.

**Core**
- `ApprovalLoop` — propose, poll, read, record, ask, execute, confirm, receipt, nudge, sweep.
- Abeyance lifecycle: the proposing process exits; any host with the store resumes.
- `poll()` is deterministic and free of model calls by construction.

**Consent**
- `ApprovalPolicy`: threshold (`ALL` / N), veto, expiry, turn cap, nudge schedule,
  `silence_after_reply`, `roles_required`, `allow_self_approval`.
- Five verdicts — `APPROVED`, `REJECTED`, `DEADLOCKED`, `UNREACHABLE`, `WAITING`.
- Per-approver ledgers keyed on the reply's sender; `DEADLOCKED` writes nothing and escalates.

**Replies**
- `interpret()` → `Suggestion`, never a decision. Ranges, `all except`, blanket rejections,
  custom vocabularies; conditionals and lone affirmations marked not-confident.
- Consumed-reply tracking, and `dismiss()` for replies that are not decisions.

**Scanning loops**
- `DueGate` with cheap/expensive triggers, blocking preconditions, and a floor sweep.
- `CursorRun` — the watermark advances only when every declared precondition is satisfied.

**Adapters**
- Stores: memory, JSON file (atomic writes), Postgres.
- Transports: memory, Gmail, SMTP+IMAP, callable wrapper.
- Notifiers: Slack, webhook, console, recording, null.
- `FrozenClock` for time-dependent paths.

**Surfaces**
- `abeyance` CLI with categorical exit codes and a `inject` rehearsal command.
- `PlainTextRenderer` for digests, receipts, and escalation summaries.

124 tests, no network, no credentials. Core install has zero dependencies.
