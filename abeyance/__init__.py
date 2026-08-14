"""abeyance — approval that outlives the agent.

Durable, multi-party consent for cron, serverless, and batch agents, without adopting an agent
framework or keeping a workflow in memory.

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

from .claims import ClaimedExecution, execute_claimed
from .cursor import Cursor, CursorRun, DueGate, DueVerdict, TriggerResult
from .errors import (AlreadyExecuted, ConfigurationError, CursorNotCommittable, AbeyanceError,
                     NoApproversError, PolicyError, ProposalNotFound, TransportError,
                     UnknownApprover)
from .interpret import DEFAULT_VOCABULARY, Suggestion, Vocabulary, interpret
from .loop import (ApprovalLoop, Executor, InboundReply, NudgeResult, PollResult, ProposeResult)
from .models import (Approver, Escalation, EscalationEvent, ExecutionReport, Item, ItemOutcome,
                     Proposal, Reply, Sent, Status, Verdict)
from .policy import ALL, ANY_ONE, SINGLE_APPROVER, UNANIMOUS, ApprovalPolicy, majority
from .ports import Clock, FrozenClock, Notifier, Renderer, Store, SystemClock, Transport
from .render import PlainTextRenderer, render_escalation
from .verdict import VerdictSummary, summarize, verdict_for, verdicts

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # loop
    "ApprovalLoop", "Executor", "InboundReply", "PollResult", "ProposeResult", "NudgeResult",
    "execute_claimed", "ClaimedExecution",
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
    # render
    "PlainTextRenderer", "render_escalation",
    # errors
    "AbeyanceError", "ConfigurationError", "NoApproversError", "PolicyError",
    "ProposalNotFound", "UnknownApprover", "AlreadyExecuted", "TransportError",
    "CursorNotCommittable",
]
