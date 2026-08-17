# Architecture

## The one decision everything follows from

**Consent is separate from execution.**

Durable agent runtimes already survive process death — LangGraph resumes an `interrupt()` from
a checkpointer, Temporal takes a signal against durable workflow state. The distinction here is
not durability, it is *ownership*: the consent record is a first-class object with its own
lifecycle, not a decision a running workflow is holding open. Any worker that can reach the
store carries it forward, including one with no agent in it at all.

Every design choice below is downstream of that. A workflow holding the wait keeps continuity
in a live frame — it knows who it asked, what it has already read, and where it was. With
consent detached, none of that is implied, so a durable proposal, sender-attributed replies, a
consumed-reply set, an idempotent execute, a free polling gate, and a guarded watermark each
have to be made explicit. That is most of what this library *is*.

**Scope, before the detail:** run one apply worker at a time (see
[Concurrency](#concurrency)), and treat sender attribution as an operational control rather
than authentication.

```
┌─ propose tick ──────────────────────────────────────────────────────┐
│  DueGate.evaluate  →  gather  →  render  →  Transport.send          │
│                                              ↓                       │
│                                        Store.put(proposal)           │
│                                        CursorRun.advance()           │
└──────────────────────────────────────────────────────────────────────┘
                                   ⋮
                  no process · hours or days · possibly a redeploy
                                   ⋮
┌─ apply tick (hourly) ───────────────────────────────────────────────┐
│  poll()      Store.items + Transport.fetch      free, deterministic  │
│  read()      + interpret()                      a suggestion only    │
│  record()    per-approver ledger                explicit numbers     │
│  verdicts()  pure function of (proposal, policy)                     │
│  execute()   only APPROVED items → your executor                     │
│  receipt()   fresh thread, possibly wider audience                   │
└──────────────────────────────────────────────────────────────────────┘
```

## Modules

| Module | Owns | Depends on |
|---|---|---|
| `models.py` | `Item`, `Approver`, `Proposal`, `Status`, `Verdict`, outcomes | nothing |
| `policy.py` | thresholds, veto, expiry, turns, nudge schedule | nothing |
| `verdict.py` | **the approval math** — pure `(proposal, policy) → verdicts` | models, policy |
| `interpret.py` | free text → `Suggestion` | nothing |
| `cursor.py` | `DueGate`, `Cursor`, `CursorRun` (the watermark guard) | ports |
| `loop.py` | the orchestration and every state transition | all of the above |
| `render.py` | the words a human reads | models |
| `ports.py` | the four Protocols + clocks | models |
| `adapters/` | concrete stores, transports, notifiers | optional extras |

`verdict.py` is deliberately pure and dependency-free: "who was allowed to write this" is the
question asked after an incident, and it should be answerable by reading one screen, and
testable without standing anything up.

## Status lifecycle

```
                    ┌──────────────────┐
     propose ──────▶│  AWAITING_REPLY  │
                    └────────┬─────────┘
                             │ some approvers answered
                    ┌────────▼──────────────┐
                    │  PARTIALLY_APPROVED   │◀────┐
                    └────────┬──────────────┘     │ record()
                             │                     │
              ask() ─────────┼──────────────▶ CLARIFYING
                             │                     │  turns > max_turns
   all settled, execute()    │                     ▼
                    ┌────────▼─────┐          ┌─────────┐
                    │   EXECUTED   │          │ STALLED │
                    └──────────────┘          └─────────┘
                             │ any item deadlocked
                    ┌────────▼─────┐          ┌─────────┐
                    │  DEADLOCKED  │          │ EXPIRED │◀── sweep(), last_activity
                    └──────────────┘          └─────────┘
```

`EXECUTED`, `DEADLOCKED`, `STALLED` and `EXPIRED` are terminal. `execute()` on an already-
executed proposal raises `AlreadyExecuted` — an hourly apply tick will see the same settled
proposal repeatedly and must not double-write.

## The verdict rules, in order

For each item, given `yes` / `no` / `silent` sets over the approvers:

1. **Advisory item** → `APPROVED` (nothing to gate; never reaches an executor).
2. **No approvers** → `WAITING`. Vacuous truth would otherwise approve everything.
3. **`veto` and any `no`** → `DEADLOCKED` if anyone also said yes, else `REJECTED`.
4. **`len(yes) >= threshold`** → `APPROVED`.
5. **Threshold no longer reachable** → `REJECTED` if anyone objected, else `UNREACHABLE`.
6. Otherwise → `WAITING`.

Step 5 depends on `policy.silence_after_reply`:

- `"abstain"` (default) — an approver's reply *was* their answer, so items they passed over
  cannot reach the threshold. This is what makes an unattended loop terminate.
- `"waiting"` — they may still speak. Correct only with a human driving follow-ups; on a cron
  it means waiting forever.

`ask()` reopens the question for whoever it addresses (clears `replied_at`, keeps the ledger),
so a clarification round is never blocked by this setting.

## The four seams

Protocols, not base classes — an adapter is anything with the right methods, including the
mail helper you already have.

**Store** — `get / put / items / delete` over `(kind, key) → dict`. `items()` must be one
round trip: the poll tick calls it every run, and an N+1 there is the difference between a
cheap gate and an expensive one.

**Transport** — `address`, `send`, `fetch_replies`. Two hard requirements: every reply carries
its **sender**, and our own messages are excluded **by sender as well as by id**.

**Notifier** — `notify(channel_id, message)`. Separate from Transport because a nudge should
arrive somewhere other than the mailbox already being ignored. The cap lives in the policy, so
an adapter cannot opt out of it.

**Clock** — `now() → int`. Injectable so a seven-day expiry and a 72-hour second nudge are
testable in microseconds. Untestable time-based behaviour is untested time-based behaviour.

## Where the model goes

Nowhere inside the library, on purpose. The natural division on a run that uses one:

| Step | Who | Why |
|---|---|---|
| decide what to propose | your agent | it is the judgment you are automating |
| render the digest | `Renderer` | deterministic, and you want it diffable |
| `poll()` | library | **must stay free of model calls** — the whole cost argument |
| read a reply | `interpret()` first, model on anything not `confident` | most replies are `"approve 1,3"` |
| decide what it meant | model or human | never a regex |
| `record()` / `execute()` | library | deterministic given the decision |

<a id="concurrency"></a>
## Concurrency — claims serialize live workers, not side effects

**This library does not provide exactly-once execution across distributed workers, and should
not be described as if it does.** `PostgresStore` (and `MemoryStore` for tests) provides atomic,
expiring claims. `execute_claimed()` claims one approval execution; `claimed()` can claim a whole
case tick before it derives needs and dispatches workers. Two apply ticks racing on one proposal
(an overlapping cron, or a manual run beside the scheduled one) are therefore serializable when
the caller uses these claims. The current guarantees:

- `record()` is idempotent per approver — it replaces a ledger rather than appending, so the
  same reply processed twice reaches the same state.
- `execute()` refuses a proposal already `EXECUTED`.
- `Store.put` is last-write-wins.

Claims do not provide exactly-once *external* side effects. If an executor sends mail or charges
a card and dies before its state is saved, a later worker cannot know whether that happened.
Keep external executors idempotent on the request id. `JSONFileStore` is intentionally not
claim-capable; run a single worker when using it.

## Trust boundaries

**Sender attribution is not authentication.** An approver is identified by the `From` address
on their reply, lowercased and matched against the approver set. There is no DKIM validation,
no signature check, no proof the human named actually typed it. That is an *operational*
control — adequate inside a mailbox you administer, inadequate on its own anywhere spoofing is
in the threat model. If it is, put a verified channel in front of the transport, or use a
`CallableTransport` wrapping one that authenticates.

What the library does do at this boundary: refuse to count a decision from an address that is
not an approver (`UnknownApprover`), and raise an `UNKNOWN_SENDER` escalation when a stranger
replies to a thread rather than dropping it silently — a forwarded proposal answered by the
wrong person is exactly the case you want to hear about.

**The parser is not an authority.** `interpret()` is a convenience for the unambiguous
majority of replies. It never records. Conditional, bare-number, affirmation-only and
unparseable replies come back `confident=False`, and `record_from(require_confident=True)`
escalates rather than guessing. Consent is what a person decided, not what a regex extracted.

**Expiry moves on recorded activity only** — `record`, `dismiss`, `ask`, `confirm`, `execute`.
Fetching an inbound with `read()` or `poll()` does not move it. See
[`docs/FAILURE-MODES.md`](FAILURE-MODES.md) §3 for why, and what it costs.

## Testing

324 tests, all on in-memory adapters, no network and no credentials. The ones worth reading
first, because each pins a specific silent failure:

| Test | Failure it prevents |
|---|---|
| `test_transport_safety.py::test_our_own_message_is_never_read_as_a_reply` | reading our own proposal as consent |
| `test_transport_safety.py::test_a_recorded_reply_does_not_resurface_forever` | a loop permanently "actionable" |
| `test_lifecycle.py::test_replying_restarts_the_expiry_clock` | a live negotiation dying at the deadline |
| `test_verdict.py::test_split_is_a_deadlock_not_a_majority` | a disagreement laundered into a write |
| `test_cursor.py::test_advance_refuses_while_a_precondition_is_outstanding` | a window skipped forever, silently |
| `test_loop.py::test_execute_is_idempotent` | an hourly tick double-writing |
| `test_standing.py::test_a_recommendation_saying_approved_confers_nothing` | a model authorizing itself by wording |
| `test_standing.py::test_the_counting_layer_refuses_a_forged_row_the_constructor_never_saw` | a guard bypassed by whatever writes the row |
| `test_dispatch.py::test_a_container_that_never_booted_is_retried_and_escalated` | a lost dispatch waiting forever |
| `test_dispatch.py::test_a_worker_that_exited_cleanly_without_contributing_counts_as_failed` | exit status mistaken for success |
| `test_warrant.py::test_carry_threads_context_into_the_next_workers_spec` | a worker with no context reporting empty results |
| `test_cases.py::test_authority_is_rechecked_at_commit_time_not_trusted_from_the_row` | acting on a yes given to evidence that has changed |

## The case layer

Everything above is the approval layer. [`docs/CASES.md`](CASES.md) covers the second layer — the
same detachment applied to work with several kinds of contributor — and adds one seam to the four
described here:

**Runner** — `start / state / stop` over a worker. Deliberately primitive (image, command,
environment) so it knows nothing about cases, contributions, or authority. `FlyMachinesRunner`
uses the production isolation shape: one auto-destroyed Fly machine per contribution, running in
the capability's app and inheriting only that app's secrets. `LocalProcessRunner` is for local
development and makes no isolation claim. The runner is not responsible for retries (the
dispatcher owns those, because "should we try again" is a question about the case) and not
responsible for delivering results (a worker writes its own contribution, so one that finishes
after the dispatcher exited still counts).

The module map extends as follows, keeping the same discipline — the file that decides whether
something may happen stays pure and readable in one screen:

| Module | Owns | Depends on |
|---|---|---|
| `capability.py` | the reach ceiling: which workers exist, what each may touch | models, errors |
| `standing.py` | **the authority math** — pure `(case, contributions, policy) → Authority` | models, policy |
| `warrant.py` | monotone, one-pass rules deriving what work is warranted next | capability, models, policy |
| `dispatch.py` | the lease: did the container actually run, and what if not | capability, models, policy, ports |
| `cases.py` | the orchestration, and delegation to `ApprovalLoop` for human decisions | all of the above |

`standing.py` is to the case layer what `verdict.py` is to the approval layer, and is held to the
same standard for the same reason: "who was allowed to do this" is the question asked after an
incident.
