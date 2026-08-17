"""Core value types. Pure data — no I/O, no vendor concepts, no model calls.

Everything here is JSON round-trippable, because a proposal has to survive the thing that
makes this library different from every other human-in-the-loop approval tool: **no process
is alive while it waits.** The propose run exits. The state is the only continuity between
it and an apply run that may happen days later, on a different machine.

That constraint is why `Item.payload` is opaque. The library never learns what a proposal
*does* — it carries the payload through the wait and hands it back to a caller-supplied
executor once the approval math says it cleared. Keeping the domain out of here is what
lets one state machine serve a copy-edit approval and a production-database migration.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence


class Status(str, Enum):
    """Where a proposal is in its life.

    A str-Enum so it serializes as itself and a stored doc stays human-readable.
    """

    AWAITING_REPLY = "awaiting_reply"
    """Sent; nobody has answered yet."""

    PARTIALLY_APPROVED = "partially_approved"
    """At least one approver answered, at least one has not. Not a terminal state."""

    CLARIFYING = "clarifying"
    """We asked a follow-up question and are waiting on the answer. Counts against turns."""

    EXECUTED = "executed"
    """Terminal. The approved items ran. Re-execution is refused without `force`."""

    DEADLOCKED = "deadlocked"
    """Terminal-ish. Approvers disagreed on at least one item. Nothing was written for the
    disputed items; a human owner has to break the tie."""

    STALLED = "stalled"
    """Terminal. The conversation hit the turn cap without resolving. Escalated."""

    EXPIRED = "expired"
    """Terminal. Nobody answered in time. Nothing was written."""


OPEN_STATUSES = (Status.AWAITING_REPLY, Status.PARTIALLY_APPROVED, Status.CLARIFYING)
"""Statuses that can still absorb a reply. Everything else is settled."""


class Verdict(str, Enum):
    """The per-item outcome of the approval math. See `verdict.py` for how each is reached."""

    APPROVED = "approved"
    REJECTED = "rejected"
    DEADLOCKED = "deadlocked"
    WAITING = "waiting"
    UNREACHABLE = "unreachable"
    """Enough approvers have declined or gone silent that the threshold can no longer be met.
    Distinct from REJECTED: nobody vetoed it, it just cannot pass. Reported separately so a
    caller can re-propose rather than treat it as a decision."""


class Escalation(str, Enum):
    """Why a human owner is being pulled in. The owner is *off* the approval path and *on*
    the exception path — they are not a required approver, they are who hears about the
    cases the machine refuses to resolve on its own."""

    DEADLOCK = "deadlock"
    STALL = "stall"
    EXPIRY = "expiry"
    EXECUTION_REFUSED = "execution_refused"
    UNKNOWN_SENDER = "unknown_sender"
    AMBIGUOUS_REPLY = "ambiguous_reply"

    # -- case layer. Each of these is a way coordination itself failed, as distinct from a
    # -- decision going badly, and each one has to be loud: the shared property of all four is
    # -- that the case looks healthy from the outside while making no progress.
    DISPATCH_LOST = "dispatch_lost"
    """A worker was started and neither contributed nor is still running. Re-dispatched."""

    REQUEST_FAILED = "request_failed"
    """A request exhausted its attempts. Blocks authorization — the case cannot proceed on the
    evidence it happens to have."""

    CAPABILITY_MISSING = "capability_missing"
    """A need was warranted that no registered capability can produce. The reach ceiling, hit;
    minting a new capability is a human decision."""

    REQUEST_CAP = "request_cap"
    """The case hit `max_derived_requests`. Almost always two rules warranting each other."""

    AUTHORITY_CLAIMED = "authority_claimed"
    """A contribution asserted authority it did not have — a worker recommendation reading
    "approved", or a decision from an actor without standing for this action. Not an error, and
    not silently dropped either: a guarantee nobody can observe working is a guarantee nobody
    trusts."""

    STALE_AUTHORITY = "stale_authority"
    """Authorization was valid when granted and is not valid now. The commit-time re-check
    caught it, which is the whole reason that check exists."""


# --------------------------------------------------------------------------- items


@dataclass
class Item:
    """One numbered thing a human says yes or no to.

    `n` is the number the human types in a reply, so it is stable for the life of the
    proposal and 1-based — people do not count from zero in prose.
    """

    n: int
    summary: str
    """One line. This is what the approver actually reads before deciding."""

    payload: Dict[str, Any] = field(default_factory=dict)
    """Opaque to this library. Whatever your executor needs to carry out the item."""

    detail: str = ""
    """Optional longer rendering — evidence, a diff, the proposed text."""

    advisory: bool = False
    """Approving it changes nothing; it is a recommendation. Kept in the digest because
    hiding advisory items is how a reviewer loses the thread of what the system considered,
    but the executor is never called for one."""

    def to_doc(self) -> Dict[str, Any]:
        return {"n": self.n, "summary": self.summary, "payload": self.payload,
                "detail": self.detail, "advisory": self.advisory}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Item":
        return cls(n=int(d["n"]), summary=d.get("summary", ""), payload=d.get("payload") or {},
                   detail=d.get("detail", ""), advisory=bool(d.get("advisory")))


# --------------------------------------------------------------------------- approvers


@dataclass
class Approver:
    """One person whose answer counts, and their running ledger.

    `address` is the identity the transport authenticates a reply by — an email address for
    the mail transports. It is lowercased on construction because a reply header's casing is
    not under our control and a case-sensitive lookup silently turns a real approval into
    `unknown_sender`.
    """

    address: str
    role: str = ""
    """Free-form label — "csm", "owner", "sre". Renderers and policies may key off it."""

    display_name: str = ""
    channel_id: str = ""
    """Optional out-of-band handle for nudges (a Slack user id, a phone number)."""

    approved: List[int] = field(default_factory=list)
    rejected: List[int] = field(default_factory=list)
    replied_at: Optional[str] = None
    raw: Optional[str] = None
    """The reply verbatim. Kept because the audit question is never "what did the parser
    decide", it is "what did the person actually write"."""

    nudges: int = 0

    def __post_init__(self) -> None:
        self.address = (self.address or "").strip().lower()

    @property
    def has_replied(self) -> bool:
        return self.replied_at is not None

    def to_doc(self) -> Dict[str, Any]:
        return {"address": self.address, "role": self.role, "display_name": self.display_name,
                "channel_id": self.channel_id, "approved": sorted(self.approved),
                "rejected": sorted(self.rejected), "replied_at": self.replied_at,
                "raw": self.raw, "nudges": self.nudges}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Approver":
        return cls(address=d["address"], role=d.get("role", ""),
                   display_name=d.get("display_name", ""), channel_id=d.get("channel_id", ""),
                   approved=list(d.get("approved") or []), rejected=list(d.get("rejected") or []),
                   replied_at=d.get("replied_at"), raw=d.get("raw"), nudges=int(d.get("nudges") or 0))


# --------------------------------------------------------------------------- transport I/O


@dataclass
class Reply:
    """One inbound message, already attributed to a sender by the transport."""

    message_id: str
    sender: str
    text: str
    epoch: int = 0

    def __post_init__(self) -> None:
        self.sender = (self.sender or "").strip().lower()


@dataclass
class Sent:
    """What a transport returns after a successful send. `thread_id` is the correlation key
    for the whole conversation and becomes the proposal's id."""

    message_id: str
    thread_id: str
    header_message_id: str = ""
    """The RFC-5322 Message-ID, needed to thread a later reply. Empty for transports with no
    such concept."""


# --------------------------------------------------------------------------- proposal


@dataclass
class Proposal:
    """A batch of numbered items, a set of approvers, and everything needed to resume.

    Two timestamps, and the difference between them is load-bearing:

      `sent_epoch`           when we asked. Never moves.
      `last_activity_epoch`  when anything last happened on this thread. Expiry runs off
                             THIS one, so a conversation in progress cannot time out
                             mid-sentence. Anchoring expiry on send time is the bug that
                             kills live negotiations at the deadline.
    """

    id: str
    subject_key: str = ""
    """What the proposal is *about* in your domain — a client slug, a repo, a tenant. Used to
    correlate a proposal with its cursor, and to group in listings."""

    track: str = ""
    """Optional lane within a subject. Two tracks let one run ask different audiences for
    different levels of consent — e.g. an advisory record vs. a change to machine behaviour."""

    subject: str = ""
    """The rendered subject line, kept so a threaded reply can reuse it."""

    items: List[Item] = field(default_factory=list)
    approvers: Dict[str, Approver] = field(default_factory=dict)
    status: Status = Status.AWAITING_REPLY

    sent_epoch: int = 0
    last_activity_epoch: int = 0
    turns: int = 1

    sent_message_id: str = ""
    last_outbound_id: str = ""
    """Anchors "everything newer than our last outbound". Updated on every send, including
    clarification turns, or a multi-turn thread re-reads its own history as inbound."""

    last_outbound_header_id: str = ""
    seen_reply_ids: List[str] = field(default_factory=list)
    """Inbound message ids already ingested. Belt to `last_outbound_id`'s braces: recording a
    decision without sending anything leaves the outbound anchor unmoved, so without this the
    same reply would resurface as new on every poll."""

    context: Dict[str, Any] = field(default_factory=dict)
    """Caller's own metadata, carried through the wait untouched."""

    history: List[Dict[str, Any]] = field(default_factory=list)
    """Append-only event log for the audit trail."""

    # ----------------------------------------------------------------- helpers

    @property
    def actionable_items(self) -> List[Item]:
        """Items an executor could actually run. Advisory items are excluded here rather than
        at execute time so the approval math and the digest agree on the denominator."""
        return [i for i in self.items if not i.advisory]

    @property
    def item_numbers(self) -> set:
        return {i.n for i in self.items}

    @property
    def max_n(self) -> int:
        return max((i.n for i in self.items), default=0)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def waiting_on(self) -> List[str]:
        return sorted(a.address for a in self.approvers.values() if not a.has_replied)

    def item(self, n: int) -> Optional[Item]:
        return next((i for i in self.items if i.n == n), None)

    def approver(self, address: str) -> Optional[Approver]:
        return self.approvers.get((address or "").strip().lower())

    def log(self, event: str, **fields: Any) -> None:
        self.history.append({"event": event, "at": int(time.time()), **fields})

    def touch(self, now: Optional[int] = None) -> None:
        self.last_activity_epoch = int(now if now is not None else time.time())

    # ----------------------------------------------------------------- serialization

    def to_doc(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "subject_key": self.subject_key,
            "track": self.track,
            "subject": self.subject,
            "items": [i.to_doc() for i in self.items],
            "approvers": {k: v.to_doc() for k, v in self.approvers.items()},
            "status": self.status.value,
            "sent_epoch": self.sent_epoch,
            "last_activity_epoch": self.last_activity_epoch,
            "turns": self.turns,
            "sent_message_id": self.sent_message_id,
            "last_outbound_id": self.last_outbound_id,
            "last_outbound_header_id": self.last_outbound_header_id,
            "seen_reply_ids": list(self.seen_reply_ids),
            "context": self.context,
            "history": self.history,
        }

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Proposal":
        return cls(
            id=d["id"],
            subject_key=d.get("subject_key", ""),
            track=d.get("track", ""),
            subject=d.get("subject", ""),
            items=[Item.from_doc(x) for x in d.get("items") or []],
            approvers={k: Approver.from_doc(v) for k, v in (d.get("approvers") or {}).items()},
            status=Status(d.get("status", Status.AWAITING_REPLY.value)),
            sent_epoch=int(d.get("sent_epoch") or 0),
            last_activity_epoch=int(d.get("last_activity_epoch") or 0),
            turns=int(d.get("turns") or 1),
            sent_message_id=d.get("sent_message_id", ""),
            last_outbound_id=d.get("last_outbound_id", ""),
            last_outbound_header_id=d.get("last_outbound_header_id", ""),
            seen_reply_ids=list(d.get("seen_reply_ids") or []),
            context=d.get("context") or {},
            history=list(d.get("history") or []),
        )


# --------------------------------------------------------------------------- results


@dataclass
class ItemOutcome:
    """What an executor did with one approved item.

    Exactly one of `written` / `skipped` / `error` should be meaningful. `error` is not an
    exception: an executor that refuses an item (the document moved under it, an anchor
    vanished) is behaving correctly, and the proposal should record the refusal and carry on
    with the rest rather than abort the batch.
    """

    n: int
    written: bool = False
    skipped: str = ""
    error: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.written and not self.error

    def to_doc(self) -> Dict[str, Any]:
        return {"n": self.n, "written": self.written, "skipped": self.skipped,
                "error": self.error, "detail": self.detail}


@dataclass
class ExecutionReport:
    proposal_id: str
    dry_run: bool
    verdicts: Dict[int, Verdict]
    outcomes: List[ItemOutcome] = field(default_factory=list)
    blocked: str = ""
    """Non-empty when nothing ran — e.g. approvers still outstanding. A blocked report is not
    a failure; it is the normal answer to "is it time yet?"."""

    @property
    def written(self) -> List[ItemOutcome]:
        return [o for o in self.outcomes if o.written]

    @property
    def refused(self) -> List[ItemOutcome]:
        return [o for o in self.outcomes if o.error]

    def to_doc(self) -> Dict[str, Any]:
        return {"proposal_id": self.proposal_id, "dry_run": self.dry_run,
                "blocked": self.blocked,
                "verdicts": {str(k): v.value for k, v in self.verdicts.items()},
                "outcomes": [o.to_doc() for o in self.outcomes]}


@dataclass
class EscalationEvent:
    kind: Escalation
    proposal_id: str
    subject_key: str = ""
    detail: str = ""
    items: List[int] = field(default_factory=list)

    case_id: str = ""
    """Set by the case layer. Kept alongside `proposal_id` rather than replacing it so one
    escalation handler serves both layers — an operator wiring a pager does it once."""

    request_ids: List[str] = field(default_factory=list)
    """Which contribution requests the escalation is about, for the case-layer kinds."""

    def to_doc(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "proposal_id": self.proposal_id,
                "subject_key": self.subject_key, "detail": self.detail, "items": self.items,
                "case_id": self.case_id, "request_ids": self.request_ids}


# =========================================================================== cases
#
# Everything above is the approval layer and is unchanged. Everything below is the case
# layer: the same detachment principle applied to work that needs more than one kind of
# contributor.
#
# A `Proposal` asks humans for consent. A `Case` is the durable record that consent is one
# contribution *to*. Machines contribute evidence, models contribute recommendations, humans
# contribute decisions — each arriving from its own process, on its own schedule, with no
# process alive between them. The case is a row; the contributors are disposable.
#
# The load-bearing distinction, and the reason this is not just a shared document: a
# contribution is **typed**, and only one of those types can confer authority. See
# `standing.py` — the type and the actor decide, never the wording of the payload.


class ContributionKind(str, Enum):
    """What a contribution *is*, which decides what it can do.

    This is the whole safety model in three values. An LLM writing "approved: true" inside a
    RECOMMENDATION must never acquire authority because of what it said, so authority is
    derived from this field plus the actor's standing, and never from the payload.
    """

    EVIDENCE = "evidence"
    """An assertion about the world: an API result, a database row, a document extraction, a
    verification result. Facts, with provenance. Confers no authority ever."""

    RECOMMENDATION = "recommendation"
    """Judgment without authority: a fraud assessment, a security review, a planner's
    proposal, a model's opinion. May say "approve this" in as many words and still cannot."""

    DECISION = "decision"
    """An authoritative contribution from an actor whose standing covers the question. The
    only kind that can satisfy an authority requirement, and only then."""


class ActorKind(str, Enum):
    """Who contributed. `WORKER` is structurally incapable of deciding — see `standing.py`."""

    WORKER = "worker"
    """A container, script, or model invocation. Contributes evidence and recommendations."""

    HUMAN = "human"
    """A person, identified by the transport that carried their reply."""

    POLICY = "policy"
    """A deterministic rule acting under delegated authority — "anything under $50 is
    pre-cleared". Named explicitly so a delegated auto-decision is auditable as such rather
    than disguised as a human."""


class RequestStatus(str, Enum):
    REQUESTED = "requested"
    """Warranted, nobody sent to do it yet."""

    DISPATCHED = "dispatched"
    """A worker was started and holds a lease. If the lease expires with no contribution, the
    dispatch is presumed lost and retried — this is the branch that stops a container that
    never booted from looking like work in progress forever."""

    SATISFIED = "satisfied"
    FAILED = "failed"
    """Attempts exhausted. Terminal, escalated, and it blocks authorization: a case whose
    evidence could not be gathered must not authorize on the evidence it happens to have."""

    CANCELLED = "cancelled"
    """Withdrawn — superseded by other evidence, or the case moved on. Does not block."""


class CaseStatus(str, Enum):
    OPEN = "open"
    AUTHORIZED = "authorized"
    """Policy derived an authorization envelope. Not yet acted on, and it is re-checked at
    commit time rather than trusted — see `Authorization.still_valid`."""

    EXECUTED = "executed"
    BLOCKED = "blocked"
    """Something needs a human that policy cannot resolve: a missing capability, a request that
    failed every attempt, two decisions in conflict."""

    EXPIRED = "expired"
    ABANDONED = "abandoned"


OPEN_CASE_STATUSES = (CaseStatus.OPEN, CaseStatus.AUTHORIZED, CaseStatus.BLOCKED)
"""Statuses a tick should still look at. BLOCKED is included on purpose: the blocking
condition may be cleared by a human, and a case nobody looks at again is a case that dies
quietly."""


# --------------------------------------------------------------------------- actors


@dataclass(frozen=True)
class Actor:
    """Who is contributing, and what they are entitled to decide.

    `standing` is the list of question-labels this actor may DECIDE. It is empty for every
    worker, and `worker()` cannot set it — the constructor is the first of two guards. The
    second, in `standing.py`, is the one that matters: contributions can be written by any
    process that can reach the store (including a raw SQL insert from a shell script), so the
    authoritative check has to happen when authority is *counted*, not when it is claimed.
    """

    id: str
    kind: ActorKind = ActorKind.WORKER
    standing: tuple = ()
    display: str = ""

    @classmethod
    def worker(cls, name: str, *, display: str = "") -> "Actor":
        """A worker. Never has standing, by construction — there is no parameter for it."""
        return cls(id=f"worker:{name}", kind=ActorKind.WORKER, standing=(), display=display)

    @classmethod
    def human(cls, address: str, *, standing: Sequence[str] = (), display: str = "") -> "Actor":
        return cls(id=f"human:{address.strip().lower()}", kind=ActorKind.HUMAN,
                   standing=tuple(standing), display=display)

    @classmethod
    def policy(cls, name: str, *, standing: Sequence[str] = ()) -> "Actor":
        return cls(id=f"policy:{name}", kind=ActorKind.POLICY, standing=tuple(standing))

    @property
    def address(self) -> str:
        """The bare identity without the kind prefix — an email for a human."""
        return self.id.split(":", 1)[1] if ":" in self.id else self.id

    def to_doc(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind.value, "standing": list(self.standing),
                "display": self.display}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Actor":
        return cls(id=d["id"], kind=ActorKind(d.get("kind", ActorKind.WORKER.value)),
                   standing=tuple(d.get("standing") or ()), display=d.get("display", ""))


# --------------------------------------------------------------------------- contributions


@dataclass
class Contribution:
    """One typed thing an actor added to a case. Immutable once written.

    Stored as its **own row**, not merged into the case document. That is a deliberate
    concurrency decision: three workers finishing at the same moment would otherwise
    read-modify-write the same case doc and silently lose two contributions under
    last-write-wins. Separate rows make the contribution set append-only by construction, and
    they make the worker's side of the contract a single INSERT — which is what lets a worker
    be a shell script with `psql` and no SDK at all.
    """

    case_id: str
    kind: ContributionKind
    actor: Actor
    request_id: str = ""
    """Which request this answers. Empty means unsolicited — allowed, because a worker that
    notices something nobody asked about should be able to say so."""

    summary: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    """Opaque to this library, exactly like `Item.payload`. Never read to decide authority."""

    scope: Dict[str, Any] = field(default_factory=dict)
    """Limits the contributor is asserting — environment, spend ceiling, a date range. Merged
    into the authorization envelope, narrowing it and never widening it."""

    provenance: Dict[str, Any] = field(default_factory=dict)
    """Where it came from: source url, machine id, image digest, query, fetched_at. The audit
    question is not "what did the case conclude", it is "which fact drove it and who fetched
    that fact"."""

    dependencies: List[str] = field(default_factory=list)
    """Contribution ids this rests on. Used for commit-time staleness only: if a dependency is
    superseded, anything resting on it is stale. Version tracking, deliberately not a
    truth-maintenance system."""

    supersedes: str = ""
    """A contribution id this replaces. The superseded one stays in the record — the history is
    the point — but stops counting."""

    created_epoch: int = 0
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"{self.case_id}::{self.request_id or f'unsolicited-{self.created_epoch}'}"
        if (self.kind is ContributionKind.DECISION
                and self.actor.kind is ActorKind.WORKER):
            # First guard, and the weaker one: refuse to *construct* a worker decision. Any
            # process with store access can bypass this, which is why `standing.py` re-checks
            # at counting time. Both exist because this is the failure that must never be
            # merely unlikely.
            raise ValueError(
                f"a worker cannot contribute a DECISION ({self.actor.id} on {self.case_id}); "
                "workers contribute EVIDENCE or RECOMMENDATION — authority requires standing")

    @property
    def is_authoritative_kind(self) -> bool:
        return self.kind is ContributionKind.DECISION

    def to_doc(self) -> Dict[str, Any]:
        return {"id": self.id, "case_id": self.case_id, "kind": self.kind.value,
                "actor": self.actor.to_doc(), "request_id": self.request_id,
                "summary": self.summary, "payload": self.payload, "scope": self.scope,
                "provenance": self.provenance, "dependencies": list(self.dependencies),
                "supersedes": self.supersedes, "created_epoch": self.created_epoch}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Contribution":
        # Constructed through object.__new__-free normal path, but the DECISION guard in
        # __post_init__ would raise on a hand-forged row. That is correct for reads too: a
        # forged worker-decision row should be loud, not silently loaded and then ignored.
        return cls(case_id=d["case_id"], kind=ContributionKind(d["kind"]),
                   actor=Actor.from_doc(d.get("actor") or {"id": "unknown"}),
                   request_id=d.get("request_id", ""), summary=d.get("summary", ""),
                   payload=d.get("payload") or {}, scope=d.get("scope") or {},
                   provenance=d.get("provenance") or {},
                   dependencies=list(d.get("dependencies") or []),
                   supersedes=d.get("supersedes", ""),
                   created_epoch=int(d.get("created_epoch") or 0), id=d.get("id", ""))


# --------------------------------------------------------------------------- requests


@dataclass
class ContributionRequest:
    """"This case needs X, and here is which declared capability can produce it."

    The dispatcher's unit of work. `need` is what is wanted; `capability` is the registered
    worker that can produce it; `spec` is the freeform instruction handed to that worker at
    runtime. New *behaviour* rides in `spec` and costs nothing; new *reach* requires a new
    registered capability and therefore a human — see `capability.py`.
    """

    id: str
    need: str
    capability: str
    expects: ContributionKind = ContributionKind.EVIDENCE
    spec: Dict[str, Any] = field(default_factory=dict)

    status: RequestStatus = RequestStatus.REQUESTED
    attempts: int = 0
    dispatched_epoch: int = 0
    lease_expires_epoch: int = 0
    machine_ref: str = ""
    """The runner's handle — a Fly machine id, a pid. Kept so a lost dispatch can be
    interrogated ("is it still running, or did it never start?") rather than guessed at."""

    last_error: str = ""
    warranted_by: str = ""
    """Which rule asked for this, or "opened" if it was there from the start. This is the audit
    trail for dynamic activity selection: months later, "why did a privacy review happen on
    this case?" has to be answerable, and the answer is a rule name plus the evidence that
    triggered it."""

    optional: bool = False
    """An optional request does not block authorization if it fails. Default False, because
    "we could not gather the evidence, so we proceeded without it" is the failure this whole
    layer exists to prevent."""

    armed_at: int = 0
    """Only a contribution created at or after this moment settles the request.

    `0` for the ordinary request, asked once and answered once. It exists for the *re-probed*
    request — a fact that can change, checked again (`CaseLoop.reprobe`). Without it, re-arming
    would be pointless and quietly so: the previous run's contribution still sits in the store
    under the same request id, so the next tick would read it as already answered, mark the
    request satisfied, and never start a container. The case would then report a fresh reading it
    never took — a stale fact wearing a current timestamp, which is the exact failure this
    library exists to prevent.
    """

    @property
    def is_outstanding(self) -> bool:
        return self.status in (RequestStatus.REQUESTED, RequestStatus.DISPATCHED)

    @property
    def blocks_authorization(self) -> bool:
        if self.optional or self.status is RequestStatus.CANCELLED:
            return False
        return self.status is not RequestStatus.SATISFIED

    def to_doc(self) -> Dict[str, Any]:
        return {"id": self.id, "need": self.need, "capability": self.capability,
                "expects": self.expects.value, "spec": self.spec, "status": self.status.value,
                "attempts": self.attempts, "dispatched_epoch": self.dispatched_epoch,
                "lease_expires_epoch": self.lease_expires_epoch,
                "machine_ref": self.machine_ref, "last_error": self.last_error,
                "warranted_by": self.warranted_by, "optional": self.optional,
                "armed_at": self.armed_at}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "ContributionRequest":
        return cls(id=d["id"], need=d.get("need", ""), capability=d.get("capability", ""),
                   expects=ContributionKind(d.get("expects", ContributionKind.EVIDENCE.value)),
                   spec=d.get("spec") or {},
                   status=RequestStatus(d.get("status", RequestStatus.REQUESTED.value)),
                   attempts=int(d.get("attempts") or 0),
                   dispatched_epoch=int(d.get("dispatched_epoch") or 0),
                   lease_expires_epoch=int(d.get("lease_expires_epoch") or 0),
                   machine_ref=d.get("machine_ref", ""), last_error=d.get("last_error", ""),
                   warranted_by=d.get("warranted_by", ""), optional=bool(d.get("optional")),
                   armed_at=int(d.get("armed_at") or 0))


# --------------------------------------------------------------------------- authorization


@dataclass
class Authorization:
    """A scoped, expiring permission derived from accumulated case state.

    Not a boolean, and not stored as truth. It records what may be done, within what limits,
    resting on which contributions — and it is re-checked immediately before the side effect
    via `still_valid()`. Authority that was correct when granted and is wrong by the time it is
    used is the specific failure a long-running case invites: the human said yes on Tuesday to
    evidence that changed on Thursday.
    """

    case_id: str
    action: str
    scope: Dict[str, Any] = field(default_factory=dict)
    granted_epoch: int = 0
    expires_epoch: int = 0
    basis: List[str] = field(default_factory=list)
    """Contribution ids this authority rests on. Empty basis is a bug, not a broad licence."""

    granted_by: List[str] = field(default_factory=list)
    """Actor ids whose DECISION cleared it."""

    def still_valid(self, contributions: Sequence["Contribution"],
                    now: int) -> "tuple[bool, str]":
        """Re-derive validity against the case as it is *now*. `(ok, why_not)`.

        Three ways stale authority is caught, in the order they are cheapest to check.
        Deliberately simple version/supersede tracking rather than a general
        truth-maintenance system: the moment this needs a justification graph it should be
        an explicit dependency, not clever inference.
        """
        if self.expires_epoch and now >= self.expires_epoch:
            return False, f"authorization expired at {self.expires_epoch}"
        if not self.basis:
            return False, "authorization has no basis — refusing to treat it as a licence"
        by_id = {c.id: c for c in contributions}
        superseded = {c.supersedes for c in contributions if c.supersedes}
        for cid in self.basis:
            if cid not in by_id:
                return False, f"basis contribution {cid} is no longer present"
            if cid in superseded:
                return False, f"basis contribution {cid} has been superseded"
            for dep in by_id[cid].dependencies:
                if dep in superseded:
                    return False, (f"basis contribution {cid} depends on {dep}, which has "
                                   "been superseded")
        return True, ""

    def to_doc(self) -> Dict[str, Any]:
        return {"case_id": self.case_id, "action": self.action, "scope": self.scope,
                "granted_epoch": self.granted_epoch, "expires_epoch": self.expires_epoch,
                "basis": list(self.basis), "granted_by": list(self.granted_by)}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Authorization":
        return cls(case_id=d["case_id"], action=d.get("action", ""), scope=d.get("scope") or {},
                   granted_epoch=int(d.get("granted_epoch") or 0),
                   expires_epoch=int(d.get("expires_epoch") or 0),
                   basis=list(d.get("basis") or []), granted_by=list(d.get("granted_by") or []))


# --------------------------------------------------------------------------- the case


@dataclass
class Case:
    """The durable object no process owns.

    It holds what the work is about, what has been asked for, and what state each request is
    in. It does **not** hold the contributions — those are separate rows (see `Contribution`).
    It does not hold control flow, a program counter, or a resume point, because there is no
    program to resume: any process that can read this row can decide what happens next.

    That is the whole difference from a durable workflow. Delete the code that opened this case
    and the case is still evaluable by something written next year in another language. Delete
    a Temporal workflow definition and its in-flight instances cannot replay.
    """

    id: str
    subject_key: str = ""
    """What this is about in your domain — a client slug, a repo, an incident id."""

    action: str = ""
    """The question authority is being sought for: "launch-campaign", "pay-claim". Matched
    against an actor's `standing`, so it is a label, not prose."""

    title: str = ""
    status: CaseStatus = CaseStatus.OPEN
    requests: List[ContributionRequest] = field(default_factory=list)

    opened_epoch: int = 0
    last_activity_epoch: int = 0

    proposal_id: str = ""
    """The `ApprovalLoop` proposal carrying the human decision, if one has been asked for. The
    case layer does not reimplement consent — it delegates to the approval layer and harvests
    the result as a DECISION contribution."""

    authorization: Optional[Dict[str, Any]] = None
    """Last derived envelope, cached for audit. Never trusted: `execute()` re-derives and
    re-checks."""

    context: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)

    # ----------------------------------------------------------------- helpers

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_CASE_STATUSES

    def request(self, request_id: str) -> Optional[ContributionRequest]:
        return next((r for r in self.requests if r.id == request_id), None)

    def request_for_need(self, need: str) -> Optional[ContributionRequest]:
        return next((r for r in self.requests if r.need == need), None)

    @property
    def outstanding(self) -> List[ContributionRequest]:
        return [r for r in self.requests if r.is_outstanding]

    @property
    def blocking(self) -> List[ContributionRequest]:
        return [r for r in self.requests if r.blocks_authorization]

    def log(self, event: str, **fields: Any) -> None:
        self.history.append({"event": event, "at": int(time.time()), **fields})

    def touch(self, now: Optional[int] = None) -> None:
        self.last_activity_epoch = int(now if now is not None else time.time())

    # ----------------------------------------------------------------- serialization

    def to_doc(self) -> Dict[str, Any]:
        return {"id": self.id, "subject_key": self.subject_key, "action": self.action,
                "title": self.title, "status": self.status.value,
                "requests": [r.to_doc() for r in self.requests],
                "opened_epoch": self.opened_epoch,
                "last_activity_epoch": self.last_activity_epoch,
                "proposal_id": self.proposal_id, "authorization": self.authorization,
                "context": self.context, "history": self.history}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Case":
        return cls(id=d["id"], subject_key=d.get("subject_key", ""), action=d.get("action", ""),
                   title=d.get("title", ""),
                   status=CaseStatus(d.get("status", CaseStatus.OPEN.value)),
                   requests=[ContributionRequest.from_doc(x) for x in d.get("requests") or []],
                   opened_epoch=int(d.get("opened_epoch") or 0),
                   last_activity_epoch=int(d.get("last_activity_epoch") or 0),
                   proposal_id=d.get("proposal_id", ""), authorization=d.get("authorization"),
                   context=d.get("context") or {}, history=list(d.get("history") or []))
