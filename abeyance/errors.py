"""Errors, grouped by which way each one fails.

The distinction this library cares about: a **misconfiguration** should stop the run loudly
before anything is sent, and a **runtime disagreement with reality** (an approver who is not
on the thread, a proposal already executed) should be reported and survivable. Nothing here
is raised to signal a normal outcome — a blocked apply and a deadlock are return values, not
exceptions, because they happen on a healthy day.
"""
from __future__ import annotations


class AbeyanceError(Exception):
    """Base class, so a caller can catch everything from this library in one clause."""


# --------------------------------------------------------------------------- configuration


class ConfigurationError(AbeyanceError):
    """The loop is set up in a way that cannot work. Raised at propose time, before a message
    goes out, because the alternative is a proposal nobody can approve sitting in a mailbox
    until it expires."""


class NoApproversError(ConfigurationError):
    """A proposal with no one able to say yes. Would wait forever."""


class PolicyError(ConfigurationError):
    """Internally inconsistent policy — nudges scheduled after expiry, a nudge cap larger than
    the schedule, a threshold below one."""


# --------------------------------------------------------------------------- runtime


class ProposalNotFound(AbeyanceError):
    pass


class UnknownApprover(AbeyanceError):
    """A decision was offered for someone who is not an approver on this proposal. Refused
    rather than ignored: silently dropping it makes a mis-addressed approval look like
    silence, and the loop then nudges a person who already answered."""


class AlreadyExecuted(AbeyanceError):
    """Execution was attempted on a proposal that has already run. Idempotency is the point —
    an hourly apply tick will see the same settled proposal repeatedly, and the second run
    must not double-write."""


class TransportError(AbeyanceError):
    """The transport could not send or read. Deliberately not swallowed: a propose tick whose
    send failed must not advance a cursor, or that window's signal is lost silently."""


class CaseNotFound(AbeyanceError):
    pass


class NotCleared(AbeyanceError):
    """A model mode is not cleared to emit the contribution kind being asked of it.

    Raised by `clearance.ClearanceRegistry.require()` — and therefore at registry-build time by
    `clearance.model_capability()`. Distinct from `CapabilityMissing`, which is the *reach*
    ceiling ("no worker can touch that system"); this is the *quality* ceiling ("no recorded eval
    says this model may form that kind of contribution"). Both are answered by a human.
    """


class CapabilityMissing(ConfigurationError):
    """A case needs a contribution no registered capability can produce.

    A configuration error rather than a runtime one, and raised rather than worked around,
    because the alternatives are both worse than stopping: improvising with a worker that
    reaches somewhere else, or authorizing on the evidence that happened to arrive. The
    ceiling on what workers can reach is the security model — hitting it has to be loud.
    """


class NotAuthorized(AbeyanceError):
    """`execute()` was called on a case that has no valid authorization *right now*.

    Includes the case where authority was validly granted earlier and has since gone stale —
    expired, or resting on a contribution that has been superseded. Authority correct on
    Tuesday and wrong by Thursday is the characteristic failure of long-running work, so the
    check happens immediately before the side effect and never once, up front.
    """


class CursorNotCommittable(AbeyanceError):
    """`CursorRun.advance()` was called while a declared precondition was still unsatisfied.

    This is the structural version of a rule that is otherwise a comment nobody reads: the
    watermark may only move after both the durable write and the send have succeeded. A
    cursor that advanced without them skips that window forever, and skips it *silently*,
    which is the worst available failure mode.
    """
