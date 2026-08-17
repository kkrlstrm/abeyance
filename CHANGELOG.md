# Changelog

## 0.2.1 — unreleased

**Polling an artifact, as a first-class act.** The case layer's stated principle is "poll for
artifacts; ask only for authority", and nothing implemented the polling half. `CaseLoop.reprobe()`
re-arms one satisfied evidence request so its worker runs again — for a case waiting hours or days
on a list filling in a vendor's UI, a document being written, an invoice clearing.

- `ContributionRequest.armed_at` is what makes it real rather than a status reset: only a
  contribution created at or after the re-arming settles the request. Without it, re-arming is a
  silent no-op — the previous run's row is still under the same request id, so the next tick reads
  it as an answer and never starts a container, and the case reports a reading it never took.
- Re-probing withdraws authority immediately, before the new reading arrives. Nothing can be spent
  between "look again" and "we know what we saw".
- Refused for a `DECISION` (a machine must not re-arm a person's yes — supersede the evidence
  instead, which leaves the record intact and visibly stale) and for an external need (no worker to
  re-run; a wait is not a probe).

**Three holes found by writing those tests, all of the quiet kind:**

- **A tick could resurrect an executed case, and it executed again.** `execute()` guards on
  `status is EXECUTED`, but `_tick_one` recomputed status from authority and wrote `AUTHORIZED`
  straight over `EXECUTED`; one tick later the same case ran its executor a second time. A list
  pushed to the dialer twice, a campaign sent twice — and nothing in the record looks wrong,
  because both executions were genuinely authorized. A tick now leaves a case alone unless its
  status is open, authorized or blocked, which also stops an explicitly abandoned case from
  starting containers.

- A **delegated decision never went stale.** `policy_decision` stamped no `dependencies`, so
  "pre-cleared because suppression is verified" kept clearing after suppression stopped being
  verified. The rule fired once, on one reading, and nothing re-evaluated it. Delegated decisions
  are now stamped exactly as harvested ones are.
- **A refusal at commit time left the row claiming `AUTHORIZED`.** The stale-decision path returned
  without saving, so a case that had reached AUTHORIZED kept that label until something ticked it
  again — and if nothing did, every reader of the store saw a case cleared to act that `execute()`
  would refuse. The label now matches what `execute()` would actually do.

304 tests.

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
