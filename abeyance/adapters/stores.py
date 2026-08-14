"""Where a proposal lives while nothing is running.

Three implementations, and the choice between them is a real architectural decision rather
than a preference:

  `MemoryStore`    tests only. Dies with the process, which is exactly what the library is
                   built to survive.
  `JSONFileStore`  one file per document on local disk. Fine when exactly one machine will
                   ever run this loop.
  `PostgresStore`  shared, and the right default the moment a second host exists.

The failure mode that makes this a decision: per-host state is not merely inconvenient when
two machines run the same loop, it is *silently wrong*. Each host reads its own cursors, so a
machine that has stopped running the loop keeps a frozen snapshot forever and cannot tell
"nothing happened" from "the other host already handled it". The symptom is a digest
confidently reporting work as outstanding that was done and applied days ago, and there is no
error anywhere to notice. Filesystem state makes the *host* authoritative when the *record*
should be.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional


class MemoryStore:
    """In-process dict. For tests and examples."""

    def __init__(self) -> None:
        self._d: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._claims: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            doc = self._d.get(kind, {}).get(key)
            return json.loads(json.dumps(doc)) if doc is not None else None

    def put(self, kind: str, key: str, doc: Dict[str, Any]) -> None:
        with self._lock:
            self._d.setdefault(kind, {})[key] = json.loads(json.dumps(doc))

    def items(self, kind: str) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._d.get(kind, {})))

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._d.get(kind, {}).pop(key, None)

    def claim(self, kind: str, key: str, owner: str, *, now: int,
              lease_seconds: int = 300) -> bool:
        """Atomically acquire or renew an execution lease for one document."""
        if not owner:
            raise ValueError("claim owner must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claim_key = (kind, key)
        with self._lock:
            current = self._claims.get(claim_key)
            if current and current["owner"] != owner and current["expires_at"] > now:
                return False
            self._claims[claim_key] = {"owner": owner, "expires_at": now + lease_seconds}
            return True

    def release_claim(self, kind: str, key: str, owner: str) -> bool:
        """Release only the caller's own lease; a stale worker cannot release another's."""
        claim_key = (kind, key)
        with self._lock:
            current = self._claims.get(claim_key)
            if not current or current["owner"] != owner:
                return False
            del self._claims[claim_key]
            return True


class JSONFileStore:
    """One JSON file per document under `root/<kind>/<key>.json`.

    Writes are atomic (temp file plus rename) because the alternative — a half-written
    proposal after a crash mid-write — loses the approvals already recorded in it, and those
    are the one thing in the system that cannot be regenerated.

    This store intentionally does not implement distributed execution claims. It is a
    single-machine store; use PostgresStore when multiple workers can compete.
    """

    def __init__(self, root: os.PathLike | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, kind: str) -> Path:
        d = self.root / _safe(kind)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, kind: str, key: str) -> Path:
        return self._dir(kind) / f"{_safe(key)}.json"

    def get(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        p = self._path(kind, key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def put(self, kind: str, key: str, doc: Dict[str, Any]) -> None:
        p = self._path(kind, key)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    def items(self, kind: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for f in sorted(self._dir(kind).glob("*.json")):
            try:
                out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
        return out

    def delete(self, kind: str, key: str) -> None:
        self._path(kind, key).unlink(missing_ok=True)


class PostgresStore:
    """Shared state in one `(kind, key) -> jsonb` table. Requires the `postgres` extra.

    The table is created on first use rather than through a migration step, because several
    hosts run this code and a loop that needs a manual migration before it can start is a loop
    that will one day not start. Every write stamps `updated_by` with the host or container id,
    so "which machine last touched this" is answerable — the provenance a filesystem layout
    used to imply by location and a shared table has to record explicitly.

    A sibling claims table provides atomic, expiring execution leases. Claims prevent two live
    workers from entering the same execution window concurrently; they do not make an external
    side effect exactly-once if a worker crashes after the side effect but before proposal state
    is saved. Executors should still be idempotent for that failure boundary.
    """

    def __init__(self, dsn: str, *, schema: str = "abeyance", table: str = "state",
                 connect: Any = None) -> None:
        self.dsn = dsn
        self.schema = schema
        self.table = table
        self._connect = connect or _psycopg_connect()
        self._ensured = False

    @property
    def _qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def _claims_qualified(self) -> str:
        return f"{self.schema}.{self.table}_claims"

    def _host(self) -> str:
        app, mach = os.environ.get("FLY_APP_NAME"), os.environ.get("FLY_MACHINE_ID")
        if app:
            return f"fly:{app}/{mach or '?'}"
        for var in ("HOSTNAME", "K_SERVICE", "DYNO"):
            if os.environ.get(var):
                return os.environ[var]
        try:
            return socket.gethostname()
        except Exception:  # noqa: BLE001
            return "unknown"

    def _ensure(self, cur: Any) -> None:
        if self._ensured:
            return
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._qualified} (
                kind       text        NOT NULL,
                key        text        NOT NULL,
                doc        jsonb       NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now(),
                updated_by text,
                PRIMARY KEY (kind, key)
            )""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._claims_qualified} (
                kind       text        NOT NULL,
                key        text        NOT NULL,
                owner      text        NOT NULL,
                expires_at timestamptz NOT NULL,
                claimed_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (kind, key)
            )""")
        self._ensured = True

    def _run(self, sql: str, params: tuple = (), *, fetch: bool = False) -> Any:
        with self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                self._ensure(cur)
                cur.execute(sql, params)
                rows = cur.fetchall() if fetch else None
            conn.commit()
        return rows

    def get(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        rows = self._run(f"SELECT doc FROM {self._qualified} WHERE kind=%s AND key=%s",
                         (kind, key), fetch=True)
        return _as_doc(rows[0][0]) if rows else None

    def put(self, kind: str, key: str, doc: Dict[str, Any]) -> None:
        self._run(
            f"""INSERT INTO {self._qualified} (kind, key, doc, updated_at, updated_by)
                VALUES (%s, %s, %s::jsonb, now(), %s)
                ON CONFLICT (kind, key)
                DO UPDATE SET doc = EXCLUDED.doc,
                              updated_at = now(),
                              updated_by = EXCLUDED.updated_by""",
            (kind, key, json.dumps(doc), self._host()))

    def items(self, kind: str) -> Dict[str, Dict[str, Any]]:
        rows = self._run(f"SELECT key, doc FROM {self._qualified} WHERE kind=%s ORDER BY key",
                         (kind,), fetch=True) or []
        return {r[0]: _as_doc(r[1]) for r in rows}

    def delete(self, kind: str, key: str) -> None:
        self._run(f"DELETE FROM {self._qualified} WHERE kind=%s AND key=%s", (kind, key))

    def claim(self, kind: str, key: str, owner: str, *, now: int,
              lease_seconds: int = 300) -> bool:
        """Atomically acquire/renew an execution lease using one UPSERT.

        A competing owner can take the lease only after it has expired. The database clock is
        deliberately used for the comparison and expiry interval so workers with skewed clocks
        cannot steal a live lease from one another; `now` is accepted for Store parity/tests.
        """
        del now
        if not owner:
            raise ValueError("claim owner must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        rows = self._run(
            f"""INSERT INTO {self._claims_qualified}
                    (kind, key, owner, expires_at, claimed_at)
                VALUES (%s, %s, %s, now() + (%s * interval '1 second'), now())
                ON CONFLICT (kind, key) DO UPDATE
                    SET owner = EXCLUDED.owner,
                        expires_at = EXCLUDED.expires_at,
                        claimed_at = now()
                WHERE {self._claims_qualified}.expires_at <= now()
                   OR {self._claims_qualified}.owner = EXCLUDED.owner
                RETURNING owner""",
            (kind, key, owner, int(lease_seconds)), fetch=True) or []
        return bool(rows)

    def release_claim(self, kind: str, key: str, owner: str) -> bool:
        rows = self._run(
            f"""DELETE FROM {self._claims_qualified}
                WHERE kind=%s AND key=%s AND owner=%s
                RETURNING owner""",
            (kind, key, owner), fetch=True) or []
        return bool(rows)


# --------------------------------------------------------------------------- helpers


def _safe(s: str) -> str:
    """Filesystem-safe fragment. Keys are ids from a transport, so they can carry anything."""
    return "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in str(s))[:180] or "_"


def _as_doc(v: Any) -> Dict[str, Any]:
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def _psycopg_connect():
    try:
        import psycopg  # type: ignore
        return psycopg.connect
    except ImportError:  # pragma: no cover
        try:
            import psycopg2  # type: ignore
            return psycopg2.connect
        except ImportError:
            raise ImportError(
                "PostgresStore needs psycopg (or psycopg2). Install the extra:\n"
                '    pip install "abeyance[postgres]"\n'
                "or pass your own connect callable: PostgresStore(dsn, connect=my_connect)"
            ) from None
