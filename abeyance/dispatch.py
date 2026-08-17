"""Turning "this case needs X" into a container, and noticing when that container never ran.

This is the load-bearing file. Everything else in the case layer decides *what* should happen;
this decides whether it *actually* happened, and it is the difference between a system you can
leave alone for three days and a system you have to watch.

The failure it exists for has no error message. You ask for a worker, the platform accepts the
request, and the container never boots — bad image, no capacity, a quota, a network blip during
the pull. Nothing throws. The request sits in `DISPATCHED` looking exactly like work in
progress, and the case waits forever. Temporal solves this class of problem with task-queue
visibility timeouts and redelivery; here the equivalent is a lease:

    dispatch  ──▶  stamp (machine ref, lease expiry, attempt N)
                        │
                   lease expires with no contribution
                        │
                   ask the platform what became of it
                        │
        ┌───────────────┼────────────────┬──────────────┐
     RUNNING         EXITED           FAILED          GONE
    (blew its      (booted and     (crashed)     (never started,
     timeout)       wrote nothing)                 or reaped)
        └───────────────┴────────────────┴──────────────┘
                        │
             attempts < max?  ──▶ re-dispatch
                        │
                        └──────────▶  FAILED, escalate, block the case

Three decisions in here are the ones worth arguing about, so each is stated where it is made:

  **A satisfied request, not a clean exit, is success.** A worker that exits zero having
  written nothing has failed, and treating exit status as the signal would mark it done.

  **A worker that blows its declared lease gets killed and retried.** It could be slow rather
  than hung, and killing it wastes work — but the alternative is a case stuck forever on a
  worker nobody can see, and forever is worse than duplicated. The capability declares its own
  timeout, so the fix for a legitimately slow worker is to declare it honestly.

  **Re-dispatch is at-least-once, and the contribution write is what makes it safe.** A
  contribution is keyed `<case>::<request>`, so a duplicated worker overwrites its own row
  rather than adding a second vote. Anything a worker does *outside* the store — sending mail,
  charging a card — is not covered by that and must be idempotent on the request id. This is
  the same boundary Temporal has; it is not solved here and is not claimed to be.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .capability import Capability, CapabilityRegistry
from .models import (Case, Contribution, ContributionRequest, Escalation, EscalationEvent,
                     RequestStatus)
from .policy import CasePolicy
from .ports import Clock, RunState, Runner, SystemClock

log = logging.getLogger("abeyance.dispatch")

EnvFor = Callable[[Capability, Case, ContributionRequest], Dict[str, str]]
"""Your side of the credential grant: which secrets this worker gets for this request.

Kept as a caller-supplied callable rather than something read from the registry, because the
grant should be visible at the call site where a reviewer will look. The dispatcher adds the
`ABEYANCE_*` contract variables on top; it never invents a credential.
"""


# --------------------------------------------------------------------------- results


@dataclass
class DispatchRecord:
    request_id: str
    action: str
    """`dispatched` · `redispatched` · `satisfied` · `in-flight` · `failed` · `unmatched` ·
    `skipped`. One line per request per tick, so a log of ticks reads as a history."""

    ref: str = ""
    attempt: int = 0
    detail: str = ""

    def to_doc(self) -> Dict[str, Any]:
        return {"request_id": self.request_id, "action": self.action, "ref": self.ref,
                "attempt": self.attempt, "detail": self.detail}


@dataclass
class DispatchReport:
    records: List[DispatchRecord] = field(default_factory=list)
    escalations: List[EscalationEvent] = field(default_factory=list)
    changed: bool = False
    """True when any request's stored state moved. The caller saves only then — a tick over a
    hundred quiet cases should not rewrite a hundred rows."""

    def of(self, action: str) -> List[DispatchRecord]:
        return [r for r in self.records if r.action == action]

    @property
    def started(self) -> List[DispatchRecord]:
        return self.of("dispatched") + self.of("redispatched")

    def to_doc(self) -> Dict[str, Any]:
        return {"records": [r.to_doc() for r in self.records],
                "escalations": [e.to_doc() for e in self.escalations],
                "changed": self.changed}


# --------------------------------------------------------------------------- the dispatcher


class Dispatcher:
    """Stateless by design: it reads requests, starts containers, and writes status back.

    It holds no queue, no timers, and no memory between ticks. Everything it needs is on the
    case row, which is why killing it mid-run, redeploying it, or running it from a different
    host changes nothing. A dispatcher that kept its own idea of what was in flight would be a
    second source of truth, and the first thing to go stale.
    """

    def __init__(self, runner: Runner, registry: CapabilityRegistry, *,
                 policy: CasePolicy = CasePolicy(), clock: Optional[Clock] = None,
                 env_for: Optional[EnvFor] = None,
                 on_escalate: Optional[Callable[[EscalationEvent], None]] = None,
                 label: str = "abeyance", contribution_kind: str = "") -> None:
        self.runner = runner
        self.registry = registry
        self.policy = policy
        self.clock = clock or SystemClock()
        self.env_for = env_for
        self._escalate = on_escalate
        self.label = label
        self.contribution_kind = contribution_kind
        """The store `kind` a worker writes its contribution under. Part of the worker contract:
        a worker that has to be told where to put its answer should be told, not made to derive
        it from a naming convention it can get wrong."""

    # ----------------------------------------------------------------- collect

    @staticmethod
    def _answered(contributions: Sequence[Contribution]) -> Dict[str, int]:
        """request id -> the newest contribution epoch answering it.

        An epoch rather than a set of ids, because "has this been answered" is not quite the
        question: a re-probed request (`CaseLoop.reprobe`) is answered only by a contribution
        newer than the moment it was re-armed. The previous run's row is still there under the
        same request id, and reading it as an answer would settle a request whose container
        never ran — a stale fact presented as a current one.
        """
        newest: Dict[str, int] = {}
        for c in contributions:
            if not c.request_id:
                continue
            if c.created_epoch >= newest.get(c.request_id, -1):
                newest[c.request_id] = c.created_epoch
        return newest

    @staticmethod
    def _is_answered(req: ContributionRequest, answered: Dict[str, int]) -> bool:
        got = answered.get(req.id)
        return got is not None and got >= req.armed_at

    def collect(self, case: Case, contributions: Sequence[Contribution]) -> bool:
        """Settle every request whose contribution has arrived. Returns True if anything moved.

        Separate from `tick` and called before it, because the rules that derive new work read
        request *status*, not the raw contribution set. If collecting happened inside dispatch —
        after derivation — a rule waiting on evidence would not see it until the following tick,
        and every step of a dynamic chain would cost an extra hour for no reason.
        """
        answered = self._answered(contributions)
        changed = False
        for req in case.requests:
            if self._is_answered(req, answered) and req.status not in (
                    RequestStatus.SATISFIED, RequestStatus.CANCELLED):
                req.status = RequestStatus.SATISFIED
                req.last_error = ""
                case.log("request-satisfied", request=req.id, need=req.need)
                changed = True
        return changed

    # ----------------------------------------------------------------- the tick

    def tick(self, case: Case, contributions: Sequence[Contribution]) -> DispatchReport:
        """Advance every request on one case by at most one step. Mutates `case.requests`.

        Collect first, then dispatch. A request whose worker finished between the last tick and
        this one must settle before anything decides it is overdue, or a slow-but-successful
        worker gets duplicated on the tick after it succeeded.
        """
        report = DispatchReport()
        answered = self._answered(contributions)
        now = self.clock.now()

        for req in case.requests:
            if req.status in (RequestStatus.CANCELLED, RequestStatus.FAILED):
                report.records.append(DispatchRecord(req.id, "skipped", detail=req.status.value))
                continue

            # -- collect ---------------------------------------------------
            if self._is_answered(req, answered):
                if req.status is not RequestStatus.SATISFIED:
                    req.status = RequestStatus.SATISFIED
                    req.last_error = ""
                    case.log("request-satisfied", request=req.id, need=req.need)
                    report.changed = True
                report.records.append(DispatchRecord(req.id, "satisfied", ref=req.machine_ref))
                continue
            if req.status is RequestStatus.SATISFIED:
                # Marked satisfied but the contribution is gone — a deleted row, a restored
                # backup. Say so rather than silently re-running: the case may already have
                # acted on it.
                report.records.append(DispatchRecord(
                    req.id, "skipped", detail="marked satisfied, contribution not found"))
                continue

            # A request with no capability is satisfied out of band — the canonical case is
            # `human-decision`, which the approval layer answers. Nothing to start, and it
            # still blocks authorization until it is answered, which is the point.
            if not req.capability:
                report.records.append(DispatchRecord(
                    req.id, "external", detail=f"{req.need}: awaiting an out-of-band answer"))
                continue

            # -- dispatch or chase ----------------------------------------
            if req.status is RequestStatus.REQUESTED:
                self._start(case, req, report, now, first=True)
            elif req.status is RequestStatus.DISPATCHED:
                if now < req.lease_expires_epoch:
                    report.records.append(DispatchRecord(
                        req.id, "in-flight", ref=req.machine_ref, attempt=req.attempts,
                        detail=f"lease has {req.lease_expires_epoch - now}s left"))
                else:
                    self._lease_expired(case, req, report, now)
        return report

    # ----------------------------------------------------------------- starting

    def _start(self, case: Case, req: ContributionRequest, report: DispatchReport,
               now: int, *, first: bool) -> None:
        try:
            cap = self.registry.require(req.need)
        except Exception as e:  # noqa: BLE001 - CapabilityMissing and config errors alike
            req.status = RequestStatus.FAILED
            req.last_error = str(e)[:300]
            report.changed = True
            report.records.append(DispatchRecord(req.id, "unmatched", detail=req.last_error))
            report.escalations.append(self._raise(
                Escalation.CAPABILITY_MISSING, case, [req.id],
                f"{req.need!r}: {req.last_error}"))
            return

        env = dict(cap.env)
        if self.env_for:
            env.update(self.env_for(cap, case, req) or {})
        env.update(self._contract_env(case, req, cap))

        try:
            ref = self.runner.start(
                image=cap.image, cmd=list(cap.cmd), env=env, app=cap.app,
                entrypoint=list(cap.entrypoint),
                label=f"{self.label}-{case.id}-{req.id}"[:60],
                guest=dict(cap.guest) or None, timeout_seconds=cap.timeout_seconds)
        except Exception as e:  # noqa: BLE001
            # The platform refused. That IS an attempt — otherwise a permanently broken image
            # retries forever and the case never blocks.
            req.attempts += 1
            req.last_error = f"start failed: {e}"[:300]
            report.changed = True
            if req.attempts >= self.policy.max_attempts:
                self._fail(case, req, report, req.last_error)
            else:
                req.status = RequestStatus.REQUESTED
                report.records.append(DispatchRecord(
                    req.id, "skipped", attempt=req.attempts, detail=req.last_error))
            return

        req.attempts += 1
        req.status = RequestStatus.DISPATCHED
        req.machine_ref = ref
        req.dispatched_epoch = now
        req.lease_expires_epoch = now + self.policy.lease_for(cap.timeout_seconds)
        req.last_error = ""
        report.changed = True
        case.log("dispatched", request=req.id, need=req.need, capability=cap.name,
                 image=cap.image, ref=ref, attempt=req.attempts, runner=self.runner.name)
        report.records.append(DispatchRecord(
            req.id, "dispatched" if first else "redispatched", ref=ref, attempt=req.attempts,
            detail=f"{cap.name} → {cap.image}"))

    def _contract_env(self, case: Case, req: ContributionRequest,
                      cap: Capability) -> Dict[str, str]:
        """The variables every worker can rely on. This is the whole worker-side contract.

        Small on purpose: a worker needs to know which case and request it is answering, what
        it was asked to do, and what kind of contribution to write. Everything else — how to
        reach the store, which credentials it has — comes from the app it runs in, which is
        where the isolation boundary actually is.
        """
        return {
            "ABEYANCE_CASE_ID": case.id,
            "ABEYANCE_REQUEST_ID": req.id,
            "ABEYANCE_NEED": req.need,
            "ABEYANCE_EXPECTS": req.expects.value,
            "ABEYANCE_ACTOR": f"worker:{cap.name}",
            "ABEYANCE_SUBJECT_KEY": case.subject_key,
            "ABEYANCE_ATTEMPT": str(req.attempts + 1),
            "ABEYANCE_SPEC": json.dumps(req.spec, ensure_ascii=False),
            "ABEYANCE_CONTRIBUTION_KIND": self.contribution_kind,
            "ABEYANCE_CONTRIBUTION_KEY": f"{case.id}::{req.id}",
        }

    # ----------------------------------------------------------------- chasing

    def _lease_expired(self, case: Case, req: ContributionRequest, report: DispatchReport,
                       now: int) -> None:
        """The lease ran out and nothing was contributed. Find out why, then retry or fail."""
        try:
            state = self.runner.state(req.machine_ref)
        except Exception as e:  # noqa: BLE001 - an unreachable platform is not a failed worker
            report.records.append(DispatchRecord(
                req.id, "in-flight", ref=req.machine_ref, attempt=req.attempts,
                detail=f"state unknown ({e}) — extending rather than duplicating"))
            req.lease_expires_epoch = now + self.policy.lease_grace_seconds
            report.changed = True
            return

        if state is RunState.RUNNING:
            # Still alive past its declared timeout. Kill it: a hung worker holding a case open
            # indefinitely is worse than a duplicated one, and the capability's own
            # `timeout_seconds` is the number it agreed to.
            try:
                self.runner.stop(req.machine_ref)
            except Exception as e:  # noqa: BLE001
                log.warning("dispatch: stop(%s) failed: %s", req.machine_ref, e)
            leased = max(0, req.lease_expires_epoch - req.dispatched_epoch)
            detail = f"still running, overran its {leased}s lease; stopped"
        else:
            detail = f"lease expired, worker {state.value} with no contribution"

        report.escalations.append(self._raise(
            Escalation.DISPATCH_LOST, case, [req.id],
            f"{req.need!r} attempt {req.attempts}: {detail}"))
        case.log("dispatch-lost", request=req.id, ref=req.machine_ref, state=state.value,
                 attempt=req.attempts)
        report.changed = True

        if req.attempts >= self.policy.max_attempts:
            self._fail(case, req, report, detail)
            return
        req.status = RequestStatus.REQUESTED
        req.last_error = detail[:300]
        self._start(case, req, report, now, first=False)

    def _fail(self, case: Case, req: ContributionRequest, report: DispatchReport,
              detail: str) -> None:
        req.status = RequestStatus.FAILED
        req.last_error = detail[:300]
        report.changed = True
        case.log("request-failed", request=req.id, need=req.need, attempts=req.attempts,
                 detail=detail)
        report.records.append(DispatchRecord(req.id, "failed", ref=req.machine_ref,
                                             attempt=req.attempts, detail=detail))
        report.escalations.append(self._raise(
            Escalation.REQUEST_FAILED, case, [req.id],
            f"{req.need!r} failed after {req.attempts} attempt(s): {detail}. This blocks "
            "authorization — the case will not proceed on partial evidence."))

    # ----------------------------------------------------------------- escalation

    def _raise(self, kind: Escalation, case: Case, request_ids: Sequence[str],
               detail: str) -> EscalationEvent:
        event = EscalationEvent(kind=kind, proposal_id=case.proposal_id, case_id=case.id,
                                subject_key=case.subject_key, detail=detail,
                                request_ids=list(request_ids))
        if self._escalate:
            try:
                self._escalate(event)
            except Exception:  # noqa: BLE001 - a broken handler must not eat the tick
                log.exception("escalation handler raised for case %s", case.id)
        else:
            log.warning("escalation[%s] case=%s %s", kind.value, case.id, detail)
        return event
