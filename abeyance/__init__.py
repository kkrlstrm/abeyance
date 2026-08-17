"""abeyance — disposable workers, durable work.

Abeyance keeps a durable case outside the agent session. A case can derive the next warranted
need from arrived contributions, dispatch a registered specialist to satisfy it, and stop for a
human when it needs new reach. Workers are short-lived; the case, evidence and authority remain.

The approval layer is durable, multi-party consent for cron, serverless and batch agents, without
adopting an agent framework or keeping a workflow in memory.

Most approval systems make the agent runtime own the wait, and often that is right: durable
runtimes already survive process death — LangGraph persists an `interrupt()` through a
checkpointer, Temporal holds workflow state and takes a signal days later. If your work lives
inside one of those, use its approval primitive.

This is for the case where you do not want the runtime that asked to own the wait, or where
there is no runtime at all. It separates consent from execution:

    propose  ──▶ send ──▶ [process exits]  ··· hours or days ···  poll ──▶ record ──▶ execute

Whatever proposes renders a batch of numbered items, sends one digest, persists, and
terminates completely. The approval record, the approver identities, the partial decisions,
the expiry and the escalation then live on their own. A separate worker — another host,
another process, possibly a plain cron line with no agent in it — later applies only what
settled.

Competing apply workers can use `execute_claimed()` with a claim-capable shared store. Claims
close the live-worker race; they do not make an external side effect exactly-once if a worker
dies after performing it but before proposal state is saved. Treat sender attribution as an
operational control, not authentication.

Quickstart:

    from abeyance import ApprovalLoop, Item, Approver, UNANIMOUS
    from abeyance.adapters import MemoryStore, MemoryTransport

    loop = ApprovalLoop("deploys", store=MemoryStore(), transport=MemoryTransport(),
                        policy=UNANIMOUS)
    loop.propose(
        items=[Item(n=1, summary="Drop the legacy sessions table")],
        approvers=[Approver("dba@example.com", role="dba"),
                   Approver("lead@example.com", role="lead")],
        subject_key="prod-migration-114")

    # ... later, in a scheduled tick that costs no tokens ...
    if loop.poll():
        for pid in loop.poll().actionable:
            for inbound in loop.read(pid):
                loop.record_from(pid, inbound)
            loop.execute(pid, executor=run_migration)
"""
from __future__ import annotations

from .capability import Capability, CapabilityRegistry
from .clearance import (ClearanceRegistry, ModelClearance, model_capability)
from .cases import (HUMAN_DECISION, CaseExecution, CaseExecutor, CaseLoop, TickReport)
from .claims import ClaimedExecution, claimed, execute_claimed
from .cursor import Cursor, CursorRun, DueGate, DueVerdict, TriggerResult
from .dispatch import DispatchRecord, DispatchReport, Dispatcher, EnvFor
from .errors import (AlreadyExecuted, CapabilityMissing, CaseNotFound, ConfigurationError,
                     CursorNotCommittable, AbeyanceError, NoApproversError, NotAuthorized,
                     NotCleared, PolicyError, ProposalNotFound, TransportError,
                     UnknownApprover)
from .interpret import DEFAULT_VOCABULARY, Suggestion, Vocabulary, interpret
from .loop import (ApprovalLoop, Executor, InboundReply, NudgeResult, PollResult, ProposeResult)
from .models import (Actor, ActorKind, Approver, Authorization, Case, CaseStatus, Contribution,
                     ContributionKind, ContributionRequest, Escalation, EscalationEvent,
                     ExecutionReport, Item, ItemOutcome, Proposal, Reply, RequestStatus, Sent,
                     Status, Verdict)
from .policy import (ALL, ANY_ONE, CASE_DEFAULT, CASE_IRREVERSIBLE, SINGLE_APPROVER, UNANIMOUS,
                     ApprovalPolicy, CasePolicy, majority)
from .ports import (Clock, FrozenClock, Notifier, Renderer, RunState, Runner, Store, SystemClock,
                    Transport)
from .render import PlainTextRenderer, render_escalation
from .standing import Authority, authorize, counts_as_decision, narrow_scope
from .verdict import VerdictSummary, summarize, verdict_for, verdicts
from .warrant import CaseView, Derivation, Need, Rule, always, derive, when_payload

__version__ = "0.2.1"

__all__ = [
    "__version__",
    # loop
    "ApprovalLoop", "Executor", "InboundReply", "PollResult", "ProposeResult", "NudgeResult",
    "execute_claimed", "ClaimedExecution", "claimed",
    # models
    "Item", "Approver", "Proposal", "Reply", "Sent", "Status", "Verdict",
    "ItemOutcome", "ExecutionReport", "Escalation", "EscalationEvent",
    # policy
    "ApprovalPolicy", "UNANIMOUS", "ANY_ONE", "SINGLE_APPROVER", "majority", "ALL",
    # verdict
    "verdicts", "verdict_for", "summarize", "VerdictSummary",
    # interpret
    "interpret", "Suggestion", "Vocabulary", "DEFAULT_VOCABULARY",
    # cursor
    "DueGate", "Cursor", "CursorRun", "DueVerdict", "TriggerResult",
    # ports
    "Store", "Transport", "Notifier", "Clock", "Renderer", "SystemClock", "FrozenClock",
    "Runner", "RunState",
    # render
    "PlainTextRenderer", "render_escalation",
    # ---- the case layer -------------------------------------------------
    # cases
    "CaseLoop", "CaseExecution", "CaseExecutor", "TickReport", "HUMAN_DECISION",
    # case models
    "Case", "CaseStatus", "Contribution", "ContributionKind", "ContributionRequest",
    "RequestStatus", "Actor", "ActorKind", "Authorization",
    # capability
    "Capability", "CapabilityRegistry",
    # clearance (which model may contribute which KIND, and on what evidence)
    "ModelClearance", "ClearanceRegistry", "model_capability",
    # standing (the authority math)
    "authorize", "counts_as_decision", "narrow_scope", "Authority",
    # warrant (dynamic activity selection)
    "Rule", "Need", "CaseView", "Derivation", "derive", "always", "when_payload",
    # dispatch
    "Dispatcher", "DispatchReport", "DispatchRecord", "EnvFor",
    # case policy
    "CasePolicy", "CASE_DEFAULT", "CASE_IRREVERSIBLE",
    # errors
    "AbeyanceError", "ConfigurationError", "NoApproversError", "PolicyError",
    "ProposalNotFound", "UnknownApprover", "AlreadyExecuted", "TransportError",
    "CursorNotCommittable", "CaseNotFound", "CapabilityMissing", "NotAuthorized",
    "NotCleared",
]
