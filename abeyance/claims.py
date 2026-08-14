"""Atomic execution claims for workers that may overlap.

Consent and execution are separate concerns. This module covers the narrow hand-off between
them: before a distributed worker calls ``execute()``, it can acquire an expiring claim so a
second live worker cannot enter the same execution window concurrently.

A claim is deliberately not described as exactly-once execution. If an executor performs an
external side effect and the worker dies before proposal state is saved, a later worker cannot
know whether that side effect happened. Keep executors idempotent where that boundary matters.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .loop import ApprovalLoop, Executor
from .models import ExecutionReport


@dataclass(frozen=True)
class ClaimedExecution:
    """Result of trying to claim and execute one proposal."""

    claimed: bool
    owner: str
    report: Optional[ExecutionReport] = None


def execute_claimed(loop: ApprovalLoop, proposal_id: str, executor: Executor, *,
                    owner: str, lease_seconds: int = 300,
                    dry_run: bool = False, force: bool = False) -> ClaimedExecution:
    """Claim ``proposal_id`` atomically, then execute it.

    The configured store must provide ``claim`` and ``release_claim``. ``PostgresStore`` is
    the production implementation; ``MemoryStore`` provides the same contract for tests.
    ``JSONFileStore`` intentionally does not because it is a single-machine store.

    The claim is released after the attempt. If execution raises, it is also released so the
    proposal can be retried immediately rather than waiting for lease expiry.
    """
    if not owner:
        raise ValueError("claim owner must be non-empty")
    claim = getattr(loop.store, "claim", None)
    release = getattr(loop.store, "release_claim", None)
    if claim is None or release is None:
        raise TypeError(
            "store does not support execution claims; use PostgresStore for competing workers"
        )

    acquired = claim(loop.kind, proposal_id, owner, now=loop.clock.now(),
                     lease_seconds=lease_seconds)
    if not acquired:
        return ClaimedExecution(claimed=False, owner=owner)

    try:
        report = loop.execute(proposal_id, executor=executor, dry_run=dry_run, force=force)
        return ClaimedExecution(claimed=True, owner=owner, report=report)
    finally:
        release(loop.kind, proposal_id, owner)
