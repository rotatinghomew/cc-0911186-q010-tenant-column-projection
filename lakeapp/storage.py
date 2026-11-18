from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_batches (
    source TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    cursor INTEGER NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (source, batch_id)
);
CREATE TABLE IF NOT EXISTS source_progress (
    source TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    source TEXT NOT NULL,
    business_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (source, business_key)
);
CREATE TABLE IF NOT EXISTS manifests (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS masking_policies (
    version INTEGER PRIMARY KEY,
    rules TEXT NOT NULL,
    protected TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_versions (
    tenant TEXT NOT NULL,
    version INTEGER NOT NULL,
    columns TEXT NOT NULL,
    row_scope TEXT NOT NULL,
    masking_version INTEGER NOT NULL,
    effective_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant, version)
);
CREATE TABLE IF NOT EXISTS tenant_snapshots (
    tenant TEXT NOT NULL,
    version INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    contract_version INTEGER NOT NULL,
    masking_version INTEGER NOT NULL,
    columns TEXT NOT NULL,
    row_scope TEXT NOT NULL,
    rows TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    terms TEXT NOT NULL,
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (tenant, version)
);
CREATE TABLE IF NOT EXISTS export_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    contract_version INTEGER,
    masking_version INTEGER,
    requested_columns TEXT,
    effective_columns TEXT NOT NULL,
    row_scope TEXT NOT NULL,
    row_count INTEGER,
    snapshot_version INTEGER,
    snapshot_path TEXT,
    checksum TEXT,
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS export_runs_tenant_idx ON export_runs(tenant, id);
CREATE INDEX IF NOT EXISTS tenant_snapshots_run_idx ON tenant_snapshots(tenant, run_id);
"""


def open_store(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Add columns introduced after the first release without losing data."""

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(tenant_snapshots)")}
    if "terms" not in columns:
        connection.execute("ALTER TABLE tenant_snapshots ADD COLUMN terms TEXT NOT NULL DEFAULT '{}'")


@contextmanager
def transaction(connection: sqlite3.Connection, immediate: bool = False):
    """Yield a transaction that always uses an explicit BEGIN.

    ``immediate`` takes the write lock before the first read, which keeps a
    contract resolution and the snapshot version it produces in one serialized
    unit instead of racing another writer.
    """

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode(value: str):
    return json.loads(value)


def encode_rows(rows: list[dict]) -> bytes:
    """Serialize exactly the rows that may be delivered to a snapshot file."""

    if not rows:
        return b""
    return ("\n".join(encode(row) for row in rows) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_timestamp(value: str | datetime) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("时间不能为空")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(normalize_timestamp(value))
