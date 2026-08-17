"""The case loop: open work, gather typed contributions, derive authority, act once.

Sibling of `ApprovalLoop`, and it does not replace it. A proposal asks humans for consent; a
case is the durable thing consent is one contribution *to*. When a case needs a human decision
it delegates to an `ApprovalLoop` and harvests the answer — the email threading, the reply
attribution, the deadlock and expiry semantics are not reimplemented here, because they were
already right.

    open      ──▶ requests written to a row      [ PROCESS EXITS ]
                                                        │
    tick      ──▶ derive → dispatch containers → collect → authorize     (hourly, cheap)
                                                        │
                        ··· hours or days, nothing running ···
                                                        │
    tick      ──▶ human replies, decision harvested, authority granted
    execute   ──▶ re-check authority AT COMMIT TIME → act once → receipt

What is deliberately not here, because owning it is how this becomes a framework nobody wants:
no scheduler (bring cron), no retry policy language (one lease, one attempt count), no model
router, no memory system, no tool registry. The case layer coordinates; workers are intelligent
and disposable.

The one property everything is arranged to protect: **no process needs the previous process's
memory.** A tick is stateless, the dispatcher holds nothing between runs, and every worker
learns what it needs from environment variables and writes its answer to a row. Delete every
container image and the case is still evaluable. That is the difference from a durable
workflow, where the in-flight instance needs its own code to replay.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from .capability import CapabilityRegistry
from .dispatch import DispatchReport, Dispatcher, EnvFor
from .errors import CaseNotFound, ConfigurationError, NotAuthorized
from .loop import ApprovalLoop
from .models import (Actor, Authorization, Case, CaseStatus, Contribution, ContributionKind,
                     ContributionRequest, Escalation, EscalationEvent, Item, OPEN_CASE_STATUSES,
                     RequestStatus, Verdict)
from .policy import CasePolicy
from .ports import Clock, Runner, Store, SystemClock
from .standing import Authority, authorize, summarize
from .warrant import Derivation, Need, Rule, derive

log = logging.getLogger("abeyance.cases")

HUMAN_DECISION = "human-decision"
"""The conventional need label for "a person with standing has to decide this". Satisfied by
the approval layer, never by a container — a request with no capability is not dispatchable."""

CaseExecutor = Callable[[Case, Authorization, Sequence[Contribution]], Any]
"""Your side of the contract at the far end: given a case, the authorization that permits it,
and everything contributed, do the thing.

It receives the authorization rather than just the case, because the scope is the interesting
part — `authorization.scope` is what every contributor's limits narrowed down to, and an
executor that ignores it is acting on more authority than was granted.
"""


# --------------------------------------------------------------------------- results


@dataclass
class TickReport:
    """What one tick did to one case. Everything a scheduled runner needs to log or exit on."""

    case_id: str
    status: CaseStatus
    derivation: Optional[Derivation] = None
    dispatch: Optional[DispatchReport] = None
    authority: Optional[Authority] = None
    escalations: List[EscalationEvent] = field(default_factory=list)
    saved: bool = False
    expired: bool = False

    @property
    def authorized(self) -> bool:
        return bool(self.authority and self.authority.granted)

    @property
    def actionable(self) -> bool:
        """True when this case is ready for `execute()`. The only field a cron line needs."""
        return self.authorized and self.status is CaseStatus.AUTHORIZED

    def to_doc(self) -> Dict[str, Any]:
        return {"case_id": self.case_id, "status": self.status.value,
                "authorized": self.authorized, "actionable": self.actionable,
                "expired": self.expired, "saved": self.saved,
                "derivation": self.derivation.to_doc() if self.derivation else None,
                "dispatch": self.dispatch.to_doc() if self.dispatch else None,
                "authority": self.authority.to_doc() if self.authority else None,
                "escalations": [e.to_doc() for e in self.escalations]}


@dataclass
class CaseExecution:
    """The outcome of acting on a case. A refusal is a value, not an exception."""

    case_id: str
    authorized: bool = False
    written: bool = False
    blocked: str = ""
    error: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    authorization: Optional[Authorization] = None
    dry_run: bool = False

    def to_doc(self) -> Dict[str, Any]:
        return {"case_id": self.case_id, "authorized": self.authorized,
                "written": self.written, "blocked": self.blocked, "error": self.error,
                "detail": self.detail, "dry_run": self.dry_run,
                "authorization": self.authorization.to_doc() if self.authorization else None}


# --------------------------------------------------------------------------- the loop


class CaseLoop:
    """One named case loop: one registry, one rule set, one policy, one store namespace."""

    def __init__(
        self,
        name: str,
        *,
        store: Store,
        registry: Optional[CapabilityRegistry] = None,
        rules: Sequence[Rule] = (),
        policy: CasePolicy = CasePolicy(),
        runner: Optional[Runner] = None,
        approval: Optional[ApprovalLoop] = None,
        clock: Optional[Clock] = None,
        env_for: Optional[EnvFor] = None,
        on_escalate: Optional[Callable[[EscalationEvent], None]] = None,
    ) -> None:
        if not name:
            raise ConfigurationError("a case loop needs a name — it namespaces its state")
        try:
            policy.validate()
        except ValueError as e:
            raise ConfigurationError(str(e)) from e

        self.name = name
        self.store = store
        self.registry = registry or CapabilityRegistry()
        self.rules = list(rules)
        self.policy = policy
        self.runner = runner
        self.approval = approval
        self.clock = clock or SystemClock()
        self._escalate = on_escalate
        self.dispatcher = (
            Dispatcher(runner, self.registry, policy=policy, clock=self.clock,
                       env_for=env_for, on_escalate=on_escalate, label=name,
                       contribution_kind=f"{name}:contribution")
            if runner is not None else None)

    # ----------------------------------------------------------------- storage

    @property
    def kind(self) -> str:
        return f"{self.name}:case"

    @property
    def contribution_kind(self) -> str:
        """Contributions live under their own `kind`, one row each.

        Not nested inside the case document, and this is a correctness decision rather than a
        layout preference: three workers finishing within the same second would read-modify-write
        one case row and, under last-write-wins, two contributions would vanish with no error
        anywhere. Separate rows make the set append-only, and they reduce the worker's side of
        the contract to a single INSERT — which is what lets a worker be a shell script with
        `psql` and no SDK at all.
        """
        return f"{self.name}:contribution"

    def _now(self) -> int:
        return self.clock.now()

    def save(self, case: Case) -> None:
        self.store.put(self.kind, case.id, case.to_doc())

    def get(self, case_id: str) -> Case:
        doc = self.store.get(self.kind, case_id)
        if doc is None:
            raise CaseNotFound(f"{self.name}: no case {case_id!r}")
        return Case.from_doc(doc)

    def all(self) -> List[Case]:
        return [Case.from_doc(d) for d in self.store.items(self.kind).values()]

    def pending(self) -> List[Case]:
        return sorted((c for c in self.all() if c.is_open), key=lambda c: c.opened_epoch)

    def contributions(self, case_id: str) -> List[Contribution]:
        """Every contribution on one case, oldest first.

        One store round trip over the contribution kind, filtered by case. For a store with
        many cases this is the query to watch — `Store.items` is one round trip by contract, and
        a prefix-aware store can do better.
        """
        out = []
        for doc in self.store.items(self.contribution_kind).values():
            if doc.get("case_id") == case_id:
                out.append(Contribution.from_doc(doc))
        return sorted(out, key=lambda c: (c.created_epoch, c.id))

    def summary(self, case_id: str) -> Dict[str, Any]:
        case = self.get(case_id)
        return summarize(case, self.contributions(case_id))

    # ----------------------------------------------------------------- opening

    def open(
        self,
        *,
        action: str,
        subject_key: str = "",
        title: str = "",
        needs: Sequence[Union[str, Need]] = (),
        case_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Case:
        """Create a case and record what it needs from the start.

        `action` is the label authority will be checked against, so it has to match the
        `standing` a decider carries. It is a label and not prose for exactly that reason —
        "launch-campaign", not "decide whether we should launch the campaign".
        """
        if not action:
            raise ConfigurationError(
                "a case needs an `action` — it is what standing is checked against, and a case "
                "with no action can never be authorized by anyone")
        now = self._now()
        cid = case_id or f"{subject_key or 'case'}-{now}-{secrets.token_hex(3)}"
        if self.store.get(self.kind, cid) is not None:
            raise ConfigurationError(f"{self.name}: case {cid!r} already exists")

        case = Case(id=cid, subject_key=subject_key, action=action,
                    title=title or f"{action} — {subject_key}".strip(" —"),
                    status=CaseStatus.OPEN, opened_epoch=now, last_activity_epoch=now,
                    context=dict(context or {}))
        for n in needs:
            need = Need(n) if isinstance(n, str) else n
            case.requests.append(self._request_for(need, warranted_by="opened"))
        case.log("opened", action=action, subject_key=subject_key,
                 requests=[r.id for r in case.requests])
        self.save(case)
        return case

    def _request_for(self, need: Need, *, warranted_by: str) -> ContributionRequest:
        if need.external:
            return ContributionRequest(
                id=need.id_for(), need=need.need, capability="",
                expects=need.expects or ContributionKind.DECISION, spec=dict(need.spec),
                status=RequestStatus.REQUESTED, warranted_by=warranted_by,
                optional=need.optional)
        cap = self.registry.require(need.need)
        return ContributionRequest(
            id=need.id_for(), need=need.need, capability=cap.name,
            expects=need.expects or cap.emits, spec=dict(need.spec),
            status=RequestStatus.REQUESTED, warranted_by=warranted_by,
            optional=need.optional)

    def request(self, case_id: str, need: Union[str, Need], *,
                warranted_by: str = "manual") -> Case:
        """Add a request by hand. For an operator who knows something the rules do not."""
        case = self.get(case_id)
        n = Need(need) if isinstance(need, str) else need
        if case.request_for_need(n.need):
            return case
        case.requests.append(self._request_for(n, warranted_by=warranted_by))
        case.touch(self._now())
        case.log("requested", need=n.need, by=warranted_by)
        self.save(case)
        return case

    def reprobe(self, case_id: str, need: str, *, spec: Optional[Dict[str, Any]] = None,
                because: str = "") -> Case:
        """Ask a *fact* again. Re-arms one satisfied request so its worker runs a second time.

        This is the mechanism behind "poll for artifacts; ask only for authority". A case can wait
        days on something a human is building elsewhere — a list filling in a vendor's UI, a
        document being written, an invoice being paid — and the honest way to learn it is done is
        to look again. Rules cannot express that: a rule may only ADD a need, and re-asking is not
        a new need, it is the same need at a later time. So it lives here, as an explicit act by
        whatever is driving the clock.

        Three constraints, each load-bearing:

        **Facts only.** A request whose `expects` is `DECISION` is refused. Re-arming a human's
        decision would silently discard it and re-open the question — and worse, it would be a
        *machine* deciding a human's yes no longer counts. When a decision genuinely must be taken
        again, the honest path is superseding the evidence it rested on, which makes it go stale
        and be visibly not counted (`standing.authorize`). Nothing gets erased.

        **The old reading survives.** Re-probing does not delete the previous contribution. The
        worker writes the new one with `supersedes` set, so both rows persist and any decision
        resting on the old reading goes stale rather than silently applying to a world that moved.
        `armed_at` is what makes the new row necessary: until one arrives that is newer than this
        moment, the request is outstanding.

        **It costs a dispatch.** Attempts reset, so a probe that has been failing gets its full
        allowance again. That is correct — the previous failure was about the previous world — but
        it means an unbounded caller can spend unboundedly. Cadence belongs to the caller;
        `Case.context` is the natural place to record when the last probe was taken.
        """
        case = self.get(case_id)
        req = case.request_for_need(need)
        if req is None:
            raise ConfigurationError(f"case {case_id} has no request for need {need!r}")
        if req.expects is ContributionKind.DECISION:
            raise ConfigurationError(
                f"{need!r} expects a DECISION and cannot be re-probed. Re-asking a person is "
                "ask_humans(); making an existing yes stop counting is superseding the evidence "
                "it rested on, which leaves the record intact and visibly stale.")
        if not req.capability:
            raise ConfigurationError(
                f"{need!r} is answered out of band and has no capability to re-run. An external "
                "need is a wait, not a probe — poll with a need that has a worker.")

        now = self._now()
        was = req.status
        req.status = RequestStatus.REQUESTED
        req.armed_at = now
        req.attempts = 0
        req.dispatched_epoch = 0
        req.lease_expires_epoch = 0
        req.machine_ref = ""
        req.last_error = ""
        if spec is not None:
            req.spec = dict(spec)
        case.log("reprobed", request=req.id, need=need, was=was.value, because=because[:200])
        case.touch(now)
        self.save(case)
        log.info("reprobe %s %s (was %s)", case_id, need, was.value)
        return case

    # ----------------------------------------------------------------- contributing

    def contribute(
        self,
        case_id: str,
        *,
        kind: ContributionKind,
        actor: Actor,
        request_id: str = "",
        summary: str = "",
        payload: Optional[Dict[str, Any]] = None,
        scope: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, Any]] = None,
        dependencies: Sequence[str] = (),
        supersedes: str = "",
        revision: str = "",
        _allow_decision: bool = False,
    ) -> Contribution:
        """Write one contribution. The worker-facing call, and the only one a worker needs.

        It writes the contribution row and **does not touch the case row**. That is what makes
        concurrent workers safe, and it means a worker needs no read access to the case at all —
        only the ability to insert. The next tick discovers the row and marks the request
        satisfied.

        DECISION is refused here unless it comes from `harvest()`. Decisions enter through the
        approval layer, where a reply is attributed to a sender by the transport; a decision
        appearing from a worker path would be authority arriving through the door meant for
        facts. See the trust-boundary note in `docs/CASES.md` — a process that can write
        arbitrary rows to the store can still forge one, and the mitigation for that is a
        write-scoped store credential, not a check in this function.
        """
        if kind is ContributionKind.DECISION and not _allow_decision:
            raise ConfigurationError(
                "decisions do not enter through contribute() — they are harvested from the "
                "approval layer, where the transport attributes a reply to a sender. Use "
                "ask_humans() then harvest(), or policy_decision() for delegated authority.")

        now = self._now()
        # A contribution id is normally `<case>::<request>`, which is what makes an at-least-once
        # re-dispatch overwrite its own row instead of voting twice. But a worker re-running the
        # same check and reaching a DIFFERENT conclusion must not erase the reading the human was
        # shown — the whole supersession model needs both rows to exist. So when `supersedes` is
        # set we mint a distinct id. The two cases are told apart by exactly the right signal: a
        # retry supersedes nothing; a revision does.
        cid = ""
        if supersedes:
            cid = f"{case_id}::{request_id or 'unsolicited'}::{revision or f'r{now}'}"
        c = Contribution(
            case_id=case_id, kind=kind, actor=actor, request_id=request_id, summary=summary,
            payload=dict(payload or {}), scope=dict(scope or {}),
            provenance=dict(provenance or {}), dependencies=list(dependencies),
            supersedes=supersedes, created_epoch=now, id=cid)
        self.store.put(self.contribution_kind, c.id, c.to_doc())
        log.info("contribution %s (%s) by %s", c.id, c.kind.value, c.actor.id)
        return c

    def policy_decision(self, case_id: str, *, rule: str, standing: Sequence[str],
                        approve: bool = True, summary: str = "",
                        payload: Optional[Dict[str, Any]] = None,
                        request_id: str = HUMAN_DECISION) -> Contribution:
        """A decision made by a deterministic rule under explicitly delegated authority.

        This is "anything under $50 is pre-cleared", and it is a real decision — so it is
        recorded as `ActorKind.POLICY` rather than dressed up as a person. Six months later the
        audit question "who approved this?" answers "a delegation named `small-spend`", which is
        the truth and is reviewable. Delegating to a rule is fine; hiding that you did is not.
        """
        body = {"decision": "approve" if approve else "reject", **(payload or {})}
        return self.contribute(
            case_id, kind=ContributionKind.DECISION,
            actor=Actor.policy(rule, standing=standing), request_id=request_id,
            summary=summary or f"delegated decision by {rule}", payload=body,
            # Stamped with the facts it rested on, exactly as a harvested human decision is.
            # Without this a delegation could never go stale, and "pre-cleared because
            # suppression is verified" would keep clearing after suppression stopped being
            # verified — the rule fired once, on one reading, and nothing re-evaluates it. A
            # delegated yes has to expire against changing facts for the same reason a person's
            # does; it is a standing instruction, not a standing exemption.
            dependencies=self._rested_on(case_id),
            provenance={"delegated": True, "rule": rule}, _allow_decision=True)

    def _rested_on(self, case_id: str) -> List[str]:
        """The LIVE facts a decision taken right now is resting on.

        Live means not superseded by something newer. The subtle error is filtering on
        `not c.supersedes` — that keeps the OLD reading and drops the revision, so every decision
        made after a supersession is born stale and the case can never recover. What matters is
        whether a fact HAS BEEN superseded, not whether it superseded something.
        """
        all_contributions = self.contributions(case_id)
        superseded_ids = {c.supersedes for c in all_contributions if c.supersedes}
        return sorted(c.id for c in all_contributions
                      if c.kind is not ContributionKind.DECISION and c.id not in superseded_ids)

    # ----------------------------------------------------------------- humans

    def ask_humans(
        self,
        case_id: str,
        *,
        summary: str,
        approvers: Sequence[Any],
        detail: str = "",
        request_id: str = HUMAN_DECISION,
        cc: Sequence[str] = (),
        dry_run: bool = False,
    ) -> Any:
        """Ask people with standing to decide, via the approval layer.

        One item, because the case is the unit — an approver is answering "may this case
        proceed", not picking from a batch. Multi-item batches are what `ApprovalLoop` is for on
        its own; here a single question keeps the mapping from reply to authority unambiguous.
        """
        if self.approval is None:
            raise ConfigurationError(
                "this CaseLoop has no ApprovalLoop — pass approval=ApprovalLoop(...) to ask a "
                "human anything")
        case = self.get(case_id)
        if not case.request(request_id):
            # No capability: not dispatchable, satisfied out of band. It still blocks
            # authorization, which is what makes "we never asked anyone" impossible to miss.
            case.requests.append(ContributionRequest(
                id=request_id, need=HUMAN_DECISION, capability="",
                expects=ContributionKind.DECISION, status=RequestStatus.REQUESTED,
                warranted_by="ask_humans"))

        result = self.approval.propose(
            items=[Item(n=1, summary=summary, detail=detail,
                        payload={"case_id": case.id, "action": case.action})],
            approvers=list(approvers), subject_key=case.subject_key or case.id,
            context={"case_id": case.id, "case_action": case.action,
                     "case_request_id": request_id},
            cc=cc, dry_run=dry_run)
        if dry_run or not result.sent:
            return result

        case.proposal_id = result.id
        case.touch(self._now())
        case.log("asked-humans", proposal=result.id,
                 approvers=[getattr(a, "address", str(a)) for a in approvers])
        self.save(case)
        return result

    def harvest(self, case_id: str, *,
                standing: Optional[Dict[str, Sequence[str]]] = None) -> List[Contribution]:
        """Turn recorded approval-layer decisions into DECISION contributions.

        Reads the proposal's per-approver ledger — already attributed to a sender by the
        transport and already recorded by `ApprovalLoop.record()` — and writes one contribution
        per approver who has answered. Idempotent: the contribution id is
        `<case>::<request>::<address>`, so running it on every tick converges instead of
        stacking duplicate votes.

        `standing` maps an approver's address to the labels they may decide. Explicit, because
        the alternative — inferring standing from a role string, or granting it to anyone who
        happens to be on the thread — is how the guarantee in `standing.py` would quietly become
        decorative.
        """
        if self.approval is None:
            raise ConfigurationError("no ApprovalLoop configured — nothing to harvest")
        case = self.get(case_id)
        if not case.proposal_id:
            return []
        proposal = self.approval.get(case.proposal_id)
        table = self.approval.summary(proposal).table
        grants = {k.strip().lower(): tuple(v) for k, v in (standing or {}).items()}

        # Prefer an unanswered human-decision request. A case can legitimately need a *second*
        # decision after its plan changes, and the second answer must not be filed against the
        # first question.
        # Which question was this proposal asking? The proposal itself records it, because a case
        # can legitimately need a SECOND decision after its plan changes, and the second answer
        # must not be filed against the first question. Guessing from the need label breaks the
        # moment a rule warrants a differently-named decision — which is exactly when it matters.
        req_id = (proposal.context or {}).get("case_request_id") or ""
        if not req_id or case.request(req_id) is None:
            req_id = next((r.id for r in case.requests
                           if r.expects is ContributionKind.DECISION
                           and r.status is not RequestStatus.SATISFIED),
                          next((r.id for r in case.requests if r.need == HUMAN_DECISION),
                               HUMAN_DECISION))

        # What the decider was looking at when they decided, stamped as `dependencies`. This is
        # what lets `standing.authorize()` later notice their yes was about a different picture
        # and stop counting it. Without it, an approval silently transfers onto evidence the
        # person never saw: the approval is genuine, the audit trail looks clean, and what they
        # approved is not what happens.
        rested_on = self._rested_on(case.id)

        out: List[Contribution] = []
        for address, approver in sorted(proposal.approvers.items()):
            if not approver.has_replied:
                continue
            held = grants.get(address)
            if not held:
                # They replied and we have no record of what they are entitled to decide.
                # Refusing to invent standing is the whole point; say so loudly.
                self._raise(Escalation.AUTHORITY_CLAIMED, case, [req_id],
                            f"{address} replied on the case thread but no standing is declared "
                            f"for them — their answer is recorded in the proposal and counts "
                            f"for nothing here. Declare standing or remove them as an approver.")
                continue

            approved = 1 in approver.approved
            rejected = 1 in approver.rejected
            if not approved and not rejected:
                continue  # replied about something else; the approval layer owns that nuance

            # A decision is an immutable historical fact about what a person was shown. Harvest
            # runs on every tick, so re-stamping an existing one would recompute `dependencies`
            # against whatever is live NOW — quietly un-staling a decision that had gone stale,
            # and rewriting the record to claim they approved on the basis of evidence that did
            # not exist yet. That destroys the guarantee and falsifies the audit trail at the
            # same time, with no error.
            #
            # `replied_at` is what distinguishes "same reply, harvested again" from "they
            # answered again after seeing more". Only the second re-stamps.
            cid = f"{case.id}::{req_id}::{address}"
            existing = self.store.get(self.contribution_kind, cid)
            if existing is not None:
                same_reply = ((existing.get("provenance") or {}).get("replied_at")
                              == approver.replied_at)
                if same_reply:
                    continue

            verdict = table.get(1, Verdict.WAITING)
            c = Contribution(
                case_id=case.id, kind=ContributionKind.DECISION,
                actor=Actor.human(address, standing=held, display=approver.display_name),
                request_id=req_id,
                summary=f"{'approved' if approved else 'rejected'} by {address}",
                payload={"decision": "approve" if approved else "reject",
                         "item_verdict": verdict.value},
                provenance={"proposal_id": proposal.id, "replied_at": approver.replied_at,
                            "raw": (approver.raw or "")[:2000],
                            "attributed_by": "transport-sender"},
                dependencies=rested_on,
                created_epoch=self._now(),
                id=cid)
            self.store.put(self.contribution_kind, c.id, c.to_doc())
            out.append(c)
        if out:
            # Harvest is what satisfies the human-decision request, so it settles it here
            # rather than leaving that to the next dispatcher pass. Otherwise `authority()` —
            # a pure read that does not collect — would report the request as outstanding even
            # though the answer is already recorded, and a caller checking authority directly
            # would see a case blocked on a question that has been answered.
            #
            # One decision settles it. Whether ENOUGH people have decided is
            # `CasePolicy.min_deciders`, not this request's job.
            req = case.request(req_id)
            if req is not None and req.status is not RequestStatus.SATISFIED:
                req.status = RequestStatus.SATISFIED
                case.log("request-satisfied", request=req.id, need=req.need)
            case.touch(self._now())
            case.log("harvested", decisions=[c.actor.id for c in out])
            self.save(case)
        return out

    # ----------------------------------------------------------------- the tick

    def tick(self, case_id: Optional[str] = None, *,
             harvest_standing: Optional[Dict[str, Sequence[str]]] = None) -> List[TickReport]:
        """Advance every open case by one step. This is what a cron line calls.

        Order: expire, harvest, **collect**, derive, dispatch, authorize — and the order is
        load-bearing at two points. Collect before derive, so a rule sees evidence that landed
        since the last tick instead of waiting another hour for it. Derive before dispatch, so
        newly warranted work goes out on the same tick it became warranted.
        """
        cases = [self.get(case_id)] if case_id else self.pending()
        return [self._tick_one(c, harvest_standing) for c in cases]

    def _tick_one(self, case: Case,
                  harvest_standing: Optional[Dict[str, Sequence[str]]]) -> TickReport:
        now = self._now()
        report = TickReport(case_id=case.id, status=case.status)
        dirty = False

        # A case that is finished, expired or abandoned is history, not a slot. `pending()` already
        # filters these out, but `tick(case_id)` takes whatever id it is handed — and without this
        # guard an explicit tick on a closed case would derive fresh needs and START CONTAINERS
        # against it. Spending money on work somebody deliberately closed, quietly, is exactly the
        # class of failure this layer exists to prevent.
        if case.status not in OPEN_CASE_STATUSES:
            return report

        if self._expire_if_due(case, now):
            report.status, report.expired, report.saved = case.status, True, True
            return report

        if self.approval is not None and case.proposal_id and harvest_standing is not None:
            if self.harvest(case.id, standing=harvest_standing):
                case = self.get(case.id)  # harvest saved; re-read so we build on it

        contributions = self.contributions(case.id)

        # -- collect FIRST. Rules read request status, so evidence that landed since the last
        # -- tick has to be settled before derivation runs — otherwise every step of a dynamic
        # -- chain waits an extra tick for no reason.
        if self.dispatcher is not None and self.dispatcher.collect(case, contributions):
            dirty = True

        # -- derive: what does the current state warrant that has not been asked for?
        if self.rules:
            d = derive(case, contributions, self.rules, self.registry, self.policy)
            report.derivation = d
            if d.new_requests:
                case.requests.extend(d.new_requests)
                for r in d.new_requests:
                    case.log("warranted", request=r.id, need=r.need, by=r.warranted_by)
                dirty = True
            if d.unmatched:
                case.status = CaseStatus.BLOCKED
                dirty = True
                report.escalations.append(self._raise(
                    Escalation.CAPABILITY_MISSING, case, d.unmatched,
                    f"needs with no registered capability: {d.unmatched}. Minting one is a "
                    "human decision; the case is blocked until then."))
            if d.capped:
                case.status = CaseStatus.BLOCKED
                dirty = True
                report.escalations.append(self._raise(
                    Escalation.REQUEST_CAP, case, d.capped,
                    f"hit max_derived_requests={self.policy.max_derived_requests}; dropped "
                    f"{d.capped}. Two rules warranting each other is the usual cause."))

        # -- dispatch: start containers for anything outstanding, chase anything lost
        if self.dispatcher is not None:
            report.dispatch = self.dispatcher.tick(case, contributions)
            report.escalations.extend(report.dispatch.escalations)
            dirty = dirty or report.dispatch.changed

        # -- authorize: is there enough, from the right people, to act?
        report.authority = authorize(case, contributions, self.policy, now=now)
        if report.authority.ignored_claims:
            # Observable on purpose. A guarantee whose operation is invisible is a guarantee
            # nobody believes, so every contribution that tried to confer authority and failed
            # gets named once, on the tick it first counted for nothing.
            #
            # **Once** is the operative word, and the first version did not honour it: it raised on
            # every tick, so one ignored claim on a case that lives three weeks produced hundreds
            # of identical alerts and the channel that exists to make the guarantee visible became
            # the one everybody filters. Same treatment as the stale-decision branch below.
            seen = {tuple(h.get("claims") or []) for h in case.history
                    if h.get("event") == "authority-claimed"}
            key = tuple(sorted(report.authority.ignored_claims))
            if key not in seen:
                case.log("authority-claimed", claims=list(key))
                dirty = True
                report.escalations.append(self._raise(
                    Escalation.AUTHORITY_CLAIMED, case, report.authority.ignored_claims,
                    f"{len(report.authority.ignored_claims)} contribution(s) asserted authority "
                    f"without standing and were not counted: {report.authority.ignored_claims}"))

        # A request that exhausted its attempts can never be satisfied by waiting, so a case
        # holding one is not OPEN — it needs a human to fix the worker, cancel the request, or
        # mark it optional. Reporting it as OPEN is the precise failure this layer exists to
        # prevent: a case that looks like it is making progress and never will.
        stuck = [r.id for r in case.requests
                 if r.status is RequestStatus.FAILED and r.blocks_authorization]

        if report.authority.stale_decisions:
            # Once per set of stale ids, not once per tick — an alert that repeats hourly for a
            # week is an alert somebody filters.
            seen = {tuple(h.get("stale") or []) for h in case.history
                    if h.get("event") == "stale-decision"}
            key = tuple(sorted(report.authority.stale_decisions))
            if key not in seen:
                case.log("stale-decision", stale=list(key))
                dirty = True
                report.escalations.append(self._raise(
                    Escalation.STALE_AUTHORITY, case, [],
                    f"a recorded decision no longer applies: {report.authority.reason}"))

        if report.authority.conflicting or stuck:
            new_status = CaseStatus.BLOCKED
        elif report.authority.granted:
            new_status = CaseStatus.AUTHORIZED
        elif case.status is CaseStatus.AUTHORIZED:
            # Authority was withdrawn — the evidence under it changed. The case may well be
            # working the problem (a rule can warrant a replacement plan), so this is not
            # BLOCKED. But it is emphatically not AUTHORIZED, and leaving the label on would be
            # the same lie as a permanently-failed request reporting OPEN.
            new_status = CaseStatus.OPEN
        elif case.status is CaseStatus.BLOCKED and not (
                report.derivation and (report.derivation.unmatched or report.derivation.capped)):
            # Whatever blocked it may have been cleared by a human; let it flow again rather
            # than requiring someone to remember to unstick it.
            new_status = CaseStatus.OPEN
        else:
            new_status = case.status

        if new_status is not case.status:
            case.log("status", was=case.status.value, now=new_status.value,
                     why=report.authority.reason[:200])
            case.status = new_status
            dirty = True

        auth_doc = (report.authority.authorization.to_doc()
                    if report.authority.authorization else None)
        if auth_doc != case.authorization:
            case.authorization = auth_doc
            dirty = True

        if dirty:
            case.touch(now)
            self.save(case)
        report.status, report.saved = case.status, dirty
        return report

    # ----------------------------------------------------------------- acting

    def authority(self, case_id: str) -> Authority:
        """Derive authority right now. Cheap, pure, and safe to call from anything."""
        case = self.get(case_id)
        return authorize(case, self.contributions(case_id), self.policy, now=self._now())

    def execute(self, case_id: str, executor: CaseExecutor, *,
                dry_run: bool = False, strict: bool = False) -> CaseExecution:
        """Act on a case, once, and only under authority that is valid at this moment.

        The commit-time re-check is the point of this method. Authority derived on Tuesday and
        used on Thursday is the characteristic failure of long-running work: the human said yes
        to evidence, and the evidence has since been superseded. So authority is re-derived here
        rather than read from the case row, **and** the envelope that was stored when the case
        became authorized is re-validated against current state. Either check failing stops the
        write.

        A refusal is a return value with `blocked` set, not an exception, because an hourly tick
        will call this on a case that is not ready yet and that is a healthy day. Pass
        `strict=True` to raise instead.
        """
        case = self.get(case_id)
        if case.status is CaseStatus.EXECUTED:
            return CaseExecution(case_id=case.id, blocked="already executed",
                                 authorized=True, dry_run=dry_run)

        contributions = self.contributions(case.id)
        now = self._now()
        auth = authorize(case, contributions, self.policy, now=now)
        if not auth.granted or auth.authorization is None:
            if auth.stale_decisions:
                # Refusing because somebody's yes went stale is a discrete event worth waking a
                # human for: a real approval exists and is no longer applicable. A silent return
                # value here would be the "looks healthy, going nowhere" failure again.
                self._raise(Escalation.STALE_AUTHORITY, case, [],
                            f"refused to act: {auth.reason}")
            # And the row must stop claiming authority it does not have. A refusal here used to
            # return without saving, so a case that had reached AUTHORIZED kept that label until
            # something happened to tick it again — and if nothing did, every reader of the store
            # saw a case cleared to act that this method would refuse. The label has to match what
            # execute() would actually do, at the moment it declines to do it.
            if case.status is CaseStatus.AUTHORIZED:
                case.status = (CaseStatus.BLOCKED if (auth.stale_decisions or auth.conflicting)
                               else CaseStatus.OPEN)
                case.log("status", was=CaseStatus.AUTHORIZED.value, now=case.status.value,
                         why=f"refused at commit time: {auth.reason[:160]}")
                case.touch(now)
                self.save(case)
            if strict:
                raise NotAuthorized(f"{case.id}: {auth.reason}")
            return CaseExecution(case_id=case.id, blocked=auth.reason, dry_run=dry_run)

        # The stored envelope, re-validated. This is what catches a supersede that happened
        # between the tick that granted authority and now.
        if case.authorization:
            stored = Authorization.from_doc(case.authorization)
            ok, why = stored.still_valid(contributions, now)
            if not ok:
                self._raise(Escalation.STALE_AUTHORITY, case, [],
                            f"authorization granted at {stored.granted_epoch} is no longer "
                            f"valid: {why}. Nothing was written.")
                case.status = CaseStatus.BLOCKED
                case.log("stale-authority", why=why)
                case.touch(now)
                self.save(case)
                if strict:
                    raise NotAuthorized(f"{case.id}: {why}")
                return CaseExecution(case_id=case.id, authorized=False, blocked=why,
                                     authorization=stored, dry_run=dry_run)

        if dry_run:
            return CaseExecution(case_id=case.id, authorized=True, dry_run=True,
                                 authorization=auth.authorization,
                                 detail={"would_execute": True, "scope": auth.authorization.scope})

        out = CaseExecution(case_id=case.id, authorized=True,
                            authorization=auth.authorization)
        try:
            result = executor(case, auth.authorization, contributions)
        except Exception as e:  # noqa: BLE001
            # A refusal from the executor is data. The case does not become EXECUTED, so a
            # later tick can retry once whatever blocked it is fixed — and it is escalated,
            # because an executor that refuses silently is the failure mode this library is
            # shaped around.
            out.error = f"{type(e).__name__}: {e}"[:300]
            case.log("execute-refused", error=out.error)
            case.touch(now)
            self.save(case)
            self._raise(Escalation.EXECUTION_REFUSED, case, [], out.error)
            return out

        out.written = True
        out.detail = result if isinstance(result, dict) else {"result": str(result)[:500]}
        case.status = CaseStatus.EXECUTED
        case.touch(now)
        case.log("executed", by=getattr(executor, "__name__", "executor"),
                 scope=auth.authorization.scope, basis=auth.authorization.basis)
        self.save(case)
        return out

    # ----------------------------------------------------------------- housekeeping

    def sweep(self) -> Dict[str, List[str]]:
        """Expire cases nobody has contributed to. One store read; safe on any tick."""
        out: Dict[str, List[str]] = {"expired": []}
        now = self._now()
        for case in self.pending():
            if self._expire_if_due(case, now):
                out["expired"].append(case.id)
        return out

    def _expire_if_due(self, case: Case, now: int) -> bool:
        cutoff = now - self.policy.expire_after_days * 86400
        anchor = case.last_activity_epoch or case.opened_epoch
        if not anchor or anchor >= cutoff:
            return False
        case.status = CaseStatus.EXPIRED
        case.log("expired", outstanding=[r.id for r in case.outstanding])
        self.save(case)
        self._raise(Escalation.EXPIRY, case, [r.id for r in case.outstanding],
                    f"expired after {self.policy.expire_after_days:g}d with no activity; "
                    f"outstanding: {[r.id for r in case.outstanding]}")
        return True

    def _raise(self, kind: Escalation, case: Case, request_ids: Sequence[str],
               detail: str) -> EscalationEvent:
        event = EscalationEvent(kind=kind, proposal_id=case.proposal_id, case_id=case.id,
                                subject_key=case.subject_key, detail=detail,
                                request_ids=list(request_ids))
        if self._escalate:
            try:
                self._escalate(event)
            except Exception:  # noqa: BLE001
                log.exception("escalation handler raised for case %s", case.id)
        else:
            log.warning("escalation[%s] case=%s %s", kind.value, case.id, detail)
        return event
