# Failure modes

Every design choice in this library is a response to a specific way an abeyance approval loop
breaks. They share a shape: **nothing raises, nothing logs, and the system looks like it is
working.** That is what makes them expensive — you find out weeks later, from a person, not
from a monitor.

Each has the test that pins it.

---

## 1. Reading your own voice as consent

**What happens.** The propose tick sends, then crashes before writing state. The message id is
never recorded. On the next poll, the thread contains a message the state does not know about,
sent by us, containing the full proposal text — and an id-only filter treats it as an inbound
reply. The parser finds the item numbers *in our own digest* and reads them as approvals.

**Why it is silent.** Everything succeeds. Items get written. The audit trail shows an
approval. Nobody approved anything.

**The fix.** Transports exclude our own messages **by sender**, not only by id.

> `test_transport_safety.py::test_our_own_message_is_never_read_as_a_reply`

---

## 2. The reply that never stops arriving

**What happens.** Recording a decision sends nothing, so the outbound anchor does not move.
Without a consumed-message set, the same reply is "new" on every hourly poll. The loop is
permanently actionable, the apply tick fires forever, and if a model sits behind that gate you
are paying for it every hour.

**Why it is silent.** It looks like a busy loop, not a broken one.

**The fix.** `seen_reply_ids` on the proposal, plus `dismiss()` for replies that are read and
deliberately not treated as decisions — an out-of-office, a thank-you.

> `test_transport_safety.py::test_a_recorded_reply_does_not_resurface_forever`
> `test_transport_safety.py::test_dismiss_consumes_a_reply_that_is_not_a_decision`

---

## 3. The negotiation that dies at the deadline

**What happens.** Expiry anchored on send time. Two approvers spend six days going back and
forth, agree on day seven, and the proposal expires mid-conversation. The work in it is lost,
and nobody is told, because expiry is the normal end of an unanswered proposal.

**Why it is silent.** Expiring is expected behaviour. Nothing distinguishes "nobody cared" from
"they were actively talking about it".

**The fix.** Expiry runs off `last_activity_epoch`, which moves on *recorded* activity —
`record()`, `dismiss()`, `ask()`, `confirm()`, `execute()` — and **not** on merely fetching an
inbound with `read()` or `poll()`.

That narrower rule is deliberate: resetting on any inbound would let an out-of-office
auto-reply keep a dead proposal alive indefinitely. The cost is real and worth naming — an
ambiguous reply landing near the deadline needs a deliberate act to extend it. That is exactly
the case where `record_from()` raises an `AMBIGUOUS_REPLY` escalation rather than recording,
so it is surfaced to a human instead of quietly running out the clock.

Every expiry also raises an escalation, because an expiry nobody hears about is
indistinguishable from a healthy quiet week.

> `test_lifecycle.py::test_replying_restarts_the_expiry_clock`
> `test_lifecycle.py::test_expiry_settles_an_unanswered_proposal_and_says_so`

---

## 4. A disagreement laundered into a write

**What happens.** Two people with equal standing answer. One approves item 3, the other
rejects it. A system that takes the majority, the most recent, or the first, resolves a real
human disagreement by accident of implementation — and writes something one of them explicitly
objected to.

**Why it is silent.** Both replies were processed. The item was written. Everything looks
handled.

**The fix.** `DEADLOCKED` is a first-class verdict. Nothing is written, the proposal ends
deadlocked, and an owner who is deliberately *not* an approver is told.

> `test_verdict.py::test_split_is_a_deadlock_not_a_majority`
> `test_multi_approver.py::test_disagreement_deadlocks_writes_nothing_and_escalates`

---

## 5. "She answered" ≠ "she approved"

**What happens.** A five-item digest comes back with "approve 1 and 3". Items 2, 4 and 5 sit in
`WAITING` because the approver technically has not rejected them. Under unanimity they can
never pass. The proposal waits out its full expiry and dies with three real decisions inside
it.

**Why it is silent.** Waiting is a healthy state. It is indistinguishable from "she has not
read it yet" — right up until the expiry notice.

**The fix.** `UNREACHABLE` as a distinct verdict, controlled by `policy.silence_after_reply`.
Nobody vetoed those items; they simply cannot reach the threshold, and that is a different fact
worth acting on — usually by re-proposing them.

> `test_verdict.py::test_silence_on_an_item_is_never_a_rejection`
> `test_verdict.py::test_summary_is_settled_only_when_nothing_can_change`

---

## 6. The conditional read as a yes

**What happens.** "Approve 1 but can you reword the second line first." A regex sees `approve`
and `1`, records an approval, and ships text the person explicitly asked to change.

**Why it is silent.** The parse is *plausible*. It would have been right nine times out of ten.

**The fix.** `interpret()` returns a `Suggestion`, never a decision. Conditionals, bare
numbers, and lone affirmations come back `confident=False`. `record()` takes explicit numbers,
and `record_from(..., require_confident=True)` escalates rather than guessing.

> `test_interpret.py::test_conditional_is_never_confident`
> `test_loop.py::test_conditional_replies_escalate_instead_of_auto_recording`

---

## 7. The window that never existed

**What happens.** A scan-and-propose loop reads everything since its watermark, renders a
digest, fails to send it — and advances the watermark anyway. That window's signal is gone
**permanently**. Not delayed. Not retried. There is no gap in any log, because from the
system's point of view it was handled.

**Why it is silent.** This is the worst one in the list. There is no artefact to find. The only
evidence is the thing that never happened.

**The fix.** `CursorRun` makes the rule structural: declare your preconditions, and `advance()`
raises while any is outstanding. A failed run leaves the window unread, which is the
recoverable state.

> `test_cursor.py::test_advance_refuses_while_a_precondition_is_outstanding`
> `test_cursor.py::test_an_exception_inside_the_run_abandons_rather_than_advances`

---

## 8. The trigger that quietly died

**What happens.** A subject is due when its source reports something new. The token is revoked,
the channel is renamed, the integration starts returning `[]`. The subject is never due again —
and a subject that is never due looks exactly like a subject with nothing happening.

**Why it is silent.** No errors. A gradually emptying schedule reads as a quiet quarter.

**The fix.** The floor sweep, on by default. Any subject not run in `floor_days` becomes due
regardless of triggers. Broken triggers also surface in `DueVerdict.reasons` rather than being
swallowed, and a `precondition` that blocks a subject is reported distinctly from one that is
merely quiet.

> `test_cursor.py::test_the_floor_sweep_catches_a_trigger_that_has_gone_dead`
> `test_cursor.py::test_a_broken_trigger_does_not_blind_the_gate`
> `test_cursor.py::test_a_blocked_subject_is_reported_not_quietly_skipped`

---

## 9. Two hosts, two truths

**What happens.** State lives on local disk. The loop runs on a laptop and on a scheduled
worker. Each reads its own cursors. The laptop, which stopped running the loop months ago,
still holds a snapshot from the day it stopped — and cannot tell "nothing happened" from
"someone else already handled this". Run a report from it and you get confident, detailed,
completely wrong output about work that was applied days ago.

**Why it is silent.** Both hosts are internally consistent. Neither can detect the other.

**The fix.** `PostgresStore`, and the docs saying so at every seam where it matters.
Per-host state is correct only while exactly one host will ever run the loop, and that
condition tends to stop being true without anyone deciding it should.

---

## 10. One bad item takes the batch down

**What happens.** An executor raises on item 2 of 5. The exception propagates, the batch
aborts, and the approver has no idea which of their five yeses took effect. Re-running
double-writes the first item.

**Why it is silent.** From the approver's side there is just no receipt.

**The fix.** A refusal is data, not a crash. `execute()` catches per item, records the error,
carries on with the rest, and escalates the refusals. The receipt names them explicitly:
"refused — the target moved since this was drafted, so it will be re-proposed".

> `test_loop.py::test_a_refusing_item_does_not_abandon_the_rest`

---

## 11. The nudge that becomes a filter rule

**What happens.** A reminder every hour until they answer. They answer once, then filter the
sender. The *next* real proposal is never seen.

**The fix.** A nudge schedule (`nudge_after_hours`) and a hard cap, both in the policy so an
adapter cannot opt out. `validate()` refuses a schedule whose nudges fall after expiry, and one
whose cap exceeds the number of times defined — two settings that look reasonable separately
and are incoherent together. Turns are capped the same way: three, then `STALLED` and escalate.

> `test_lifecycle.py::test_nudges_fire_on_schedule_and_stop_at_the_cap`
> `test_lifecycle.py::test_turn_cap_stalls_instead_of_asking_forever`
> `test_verdict.py::test_policy_validation_rejects_incoherent_settings`

---

## 12. A proposal nobody can approve

**What happens.** A two-yes policy is handed one approver, or an approver record has no
address. The proposal is sent, waits, nudges, and expires. It could never have passed.

**The fix.** Caught at propose time, before anything is sent: `NoApproversError`,
`roles_required`, and `allow_self_approval=False` for the degenerate case where a loop asks
itself for permission.

> `test_loop.py::test_refuses_a_proposal_nobody_can_approve`
> `test_multi_approver.py::test_roles_required_catches_a_one_person_two_yes_policy`

---

## 13. The planner that is too useful to stop

**What happens.** A case gets a planner, and the planner is good. Every round it finds one more
thing worth knowing: a deliverability check, then a list-quality score, then a look at what the
competitor did. Each proposal is individually reasonable and defensible in review. Three weeks
later the case has eleven pieces of evidence, nobody has decided anything, and the window the work
was for has closed.

**Why it is silent.** Every single round looks like diligence. There is no error, no stall, no
escalation, and the case history reads as a thorough investigation. The only symptom is a date.

**The fix.** Not a better prompt — a budget that the planner does not administer. `max_plans` (2)
rounds per case ever, `max_planned_needs` (3) pieces of work across all of them, and
`max_needs_per_plan` (2) in any one round, all counted from the case's own request rows. When the
budget is spent the planner's last act is to warrant the human decision on what is already on the
record, because a planner that simply stops leaves a case that is neither progressing nor asking
for anything.

> `test_planner.py::test_a_planner_cannot_keep_a_case_open_forever`
> `test_planner.py::test_the_round_budget_is_hard_and_ends_in_a_person`
> `test_planner.py::test_the_work_budget_is_hard_across_rounds`

---

## 14. The investigation nobody needed

**What happens.** A planner proposes something that sounds obviously worth doing — "we should also
score the list quality" — and it is dispatched. The evidence arrives. It changes nothing, because
no answer it could have returned would have changed what the case does. The case is slower and the
bill is larger and the record is longer, and none of it was wrong exactly.

**Why it is silent.** The request succeeded. The evidence is real. It reads as thoroughness in
every review.

**The fix.** `changes_decision_if` is required on every proposal and checked for emptiness before
anything is dispatched. It costs nothing, it cannot be bluffed past — the question has no good
answer for the investigation nobody needs — and the reason a dropped proposal was dropped is
visible in `abeyance case-plan`.

> `test_planner.py::test_a_proposal_that_cannot_say_what_it_would_change_is_dropped`
> `test_planner.py::test_a_plan_with_nothing_usable_goes_straight_to_a_person`

---

## 15. Blocked, and quietly immortal

**What happens.** A rule warrants a need no capability produces. The case goes `BLOCKED` and
escalates — correctly. Nobody mints the worker, so the need re-derives on the next tick, and the
next. The escalation fires hourly until the channel is filtered. Worse, the unchanged case is
re-saved every tick, and saving marks it active — so a case waiting on a capability nobody was ever
going to build never expires. It sits there looking tended.

**Why it is silent.** Both halves look healthy. There is an alert (many alerts) and a case with
recent activity.

**The fix.** A capability gap is a *standing condition*, not an event: `CAPABILITY_MISSING` and
`REQUEST_CAP` escalate once per set of needs, the status stays `BLOCKED` because that stays true,
and the row is written only when something actually changed — so the case expires on schedule like
any other nobody is contributing to.

> `test_cases.py::test_a_capability_gap_is_a_standing_condition_not_an_hourly_alarm`
> `test_planner.py::test_a_case_blocked_on_a_missing_capability_still_expires`
