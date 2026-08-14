"""Execution-claim semantics: one live worker owns the apply window."""
from __future__ import annotations

import os
import threading

import pytest

from abeyance import Approver, ApprovalLoop, Item, SINGLE_APPROVER
from abeyance.adapters import JSONFileStore, MemoryStore, MemoryTransport, PostgresStore
from abeyance.claims import execute_claimed


def test_memory_claim_is_exclusive_until_release():
    store = MemoryStore()
    assert store.claim("k", "p", "worker-a", now=100, lease_seconds=30)
    assert not store.claim("k", "p", "worker-b", now=101, lease_seconds=30)
    assert store.release_claim("k", "p", "worker-a")
    assert store.claim("k", "p", "worker-b", now=102, lease_seconds=30)


def test_expired_claim_can_be_recovered_and_stale_owner_cannot_release_it():
    store = MemoryStore()
    assert store.claim("k", "p", "worker-a", now=100, lease_seconds=10)
    assert store.claim("k", "p", "worker-b", now=110, lease_seconds=10)
    assert not store.release_claim("k", "p", "worker-a")
    assert not store.claim("k", "p", "worker-c", now=111, lease_seconds=10)


def test_same_owner_can_renew_claim():
    store = MemoryStore()
    assert store.claim("k", "p", "worker-a", now=100, lease_seconds=10)
    assert store.claim("k", "p", "worker-a", now=105, lease_seconds=20)
    assert not store.claim("k", "p", "worker-b", now=115, lease_seconds=10)
    assert store.claim("k", "p", "worker-b", now=125, lease_seconds=10)


def _approved_loop(store=None):
    transport = MemoryTransport(address="bot@example.com")
    loop = ApprovalLoop("claims", store=store or MemoryStore(), transport=transport,
                        policy=SINGLE_APPROVER)
    result = loop.propose([Item(n=1, summary="ship")], [Approver("a@example.com")])
    loop.record(result.id, "a@example.com", approve=[1])
    return loop, result.id


def test_claimed_execute_runs_once_for_competing_workers():
    loop, proposal_id = _approved_loop()
    entered = threading.Event()
    finish = threading.Event()
    calls = []

    def executor(item):
        calls.append(item.n)
        entered.set()
        assert finish.wait(timeout=2)

    results = {}

    def first():
        results["a"] = execute_claimed(loop, proposal_id, executor, owner="worker-a")

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(timeout=2)
    results["b"] = execute_claimed(loop, proposal_id, executor, owner="worker-b")
    finish.set()
    thread.join(timeout=2)

    assert results["a"].claimed is True
    assert results["b"].claimed is False
    assert calls == [1]


def test_claim_is_released_when_execute_raises(monkeypatch):
    loop, proposal_id = _approved_loop()

    def boom(*args, **kwargs):
        raise RuntimeError("crash before side effect")

    monkeypatch.setattr(loop, "execute", boom)
    with pytest.raises(RuntimeError, match="crash"):
        execute_claimed(loop, proposal_id, lambda item: None, owner="worker-a")
    assert loop.store.claim(loop.kind, proposal_id, "worker-b", now=loop.clock.now(),
                            lease_seconds=30)


def test_json_store_refuses_distributed_claim_path(tmp_path):
    loop, proposal_id = _approved_loop(JSONFileStore(tmp_path / "state"))
    with pytest.raises(TypeError, match="does not support execution claims"):
        execute_claimed(loop, proposal_id, lambda item: None, owner="worker-a")


@pytest.mark.skipif(not os.environ.get("ABEYANCE_TEST_POSTGRES"),
                    reason="set ABEYANCE_TEST_POSTGRES for the real Postgres contract")
def test_postgres_claim_is_atomic_across_store_instances():
    dsn = os.environ["ABEYANCE_TEST_POSTGRES"]
    schema = "abeyance_claim_test"
    a = PostgresStore(dsn, schema=schema)
    b = PostgresStore(dsn, schema=schema)
    kind, key = "claim", "same-proposal"
    a.release_claim(kind, key, "worker-a")
    b.release_claim(kind, key, "worker-b")

    assert a.claim(kind, key, "worker-a", now=0, lease_seconds=30)
    assert not b.claim(kind, key, "worker-b", now=0, lease_seconds=30)
    assert a.release_claim(kind, key, "worker-a")
    assert b.claim(kind, key, "worker-b", now=0, lease_seconds=30)
    assert b.release_claim(kind, key, "worker-b")
