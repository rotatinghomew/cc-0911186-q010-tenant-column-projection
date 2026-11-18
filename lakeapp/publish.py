from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .derive import fact_rows
from .policy import (
    ContractError,
    apply_masking,
    contract_terms,
    get_masking_policy,
    masking_terms,
    projection,
    resolve_contract,
    row_identity,
    verify_masking,
)
from .storage import decode, digest, encode, encode_rows, normalize_timestamp, transaction, utc_now


BLOCKED_REASONS = frozenset(
    {
        "missing_contract",
        "contract_inactive",
        "missing_masking_policy",
        "invalid_tenant",
        "unauthorized_columns",
        "masking_failed",
        "write_failed",
        "invalid_contract",
    }
)


def publish(connection, name: str, version: int, rows: list[dict]) -> None:
    """Append a version manifest for a dataset that is not tenant-scoped."""

    data = encode_rows(rows)
    payload = {"rows": rows, "row_count": len(rows), "checksum": digest(data)}
    with transaction(connection, immediate=True):
        existing = connection.execute("SELECT payload FROM manifests WHERE name=? AND version=?", (name, int(version))).fetchone()
        if existing:
            if decode(existing["payload"]) != payload:
                raise ValueError("同一版本的内容冲突")
            return
        connection.execute(
            "INSERT INTO manifests(name,version,payload,status) VALUES(?,?,?,?)",
            (name, int(version), encode(payload), "ready"),
        )


def read_manifest(connection, name: str, version: int) -> dict:
    row = connection.execute("SELECT payload,status FROM manifests WHERE name=? AND version=?", (name, int(version))).fetchone()
    if not row or row["status"] != "ready":
        raise KeyError("版本不可读")
    payload = decode(row["payload"])
    if payload.get("checksum") is not None and digest(encode_rows(payload["rows"])) != payload["checksum"]:
        raise KeyError("版本校验失败")
    return payload


def _snapshot_dir(export_dir: str | Path, tenant: str) -> Path:
    return Path(export_dir) / tenant


def _stage_file(path: Path, data: bytes) -> Path:
    """Write a complete temporary file next to the final path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return Path(temporary)


def _commit_file(temporary: Path, path: Path) -> None:
    """Publish a staged file; readers never observe a partial snapshot."""

    os.replace(temporary, path)


def _record_run(connection, *, tenant: str, status: str, reason: str, detail: str, as_of: str, **fields: Any) -> int:
    cursor = connection.execute(
        "INSERT INTO export_runs(tenant,status,reason,detail,contract_version,masking_version,requested_columns,"
        "effective_columns,row_scope,row_count,snapshot_version,snapshot_path,checksum,as_of,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            tenant,
            status,
            reason,
            detail,
            fields.get("contract_version"),
            fields.get("masking_version"),
            encode(list(fields.get("requested_columns") or [])),
            encode(list(fields.get("effective_columns") or [])),
            encode(fields.get("row_scope") or {}),
            fields.get("row_count"),
            fields.get("snapshot_version"),
            fields.get("snapshot_path"),
            fields.get("checksum"),
            as_of,
            utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def _blocked(connection, tenant: str, error: ContractError, as_of: str, requested: Iterable[str] | None, run_fields: dict) -> dict:
    reason = error.reason if error.reason in BLOCKED_REASONS else "invalid_contract"
    with transaction(connection, immediate=True):
        run_id = _record_run(
            connection,
            tenant=tenant,
            status="blocked",
            reason=reason,
            detail=str(error),
            as_of=as_of,
            requested_columns=requested,
            **run_fields,
        )
    return {"status": "blocked", "tenant": tenant, "reason": reason, "detail": str(error), "run_id": run_id, "as_of": as_of}


def export_tenant(
    connection,
    tenant: str,
    export_dir: str | Path,
    as_of: str | None = None,
    columns: Iterable[str] | None = None,
    contract_version: int | None = None,
) -> dict:
    """Project, mask and write one tenant snapshot.

    Every run is appended to ``export_runs`` with the contract and masking
    version it used. A failed run is recorded as ``blocked`` with a reason and
    never produces a file, so an empty projection cannot hide a failed check.
    Prior snapshots are immutable and stay readable after a contract ends.
    """

    as_of = utc_now() if as_of is None else normalize_timestamp(as_of)
    requested = None if columns is None else tuple(sorted(set(columns)))
    run_fields: dict[str, Any] = {}

    try:
        contract = resolve_contract(connection, tenant, as_of, contract_version)
    except ContractError as error:
        return _blocked(connection, tenant, error, as_of, requested, run_fields)

    run_fields.update(
        contract_version=contract["version"],
        masking_version=contract["masking_version"],
        row_scope=contract["row_scope"].as_dict(),
    )
    try:
        # Masking is pinned by the contract version: a contract revision, not a
        # stray policy edit, is what changes how later snapshots are masked.
        policy = get_masking_policy(connection, contract["masking_version"])
        effective = projection(contract, requested)
    except ContractError as error:
        return _blocked(connection, tenant, error, as_of, requested, run_fields)

    raw_rows = [row for row in fact_rows(connection) if contract["row_scope"].matches(row)]
    raw_rows.sort(key=lambda row: (row["source"], row["business_key"]))

    try:
        masked_rows = [apply_masking(effective, row, policy) for row in raw_rows]
        verify_masking(raw_rows, masked_rows, effective, policy)
        fingerprint = digest(encode(sorted(row_identity(row) for row in raw_rows)).encode("utf-8"))
    except ContractError as error:
        return _blocked(connection, tenant, error, as_of, requested, run_fields)

    data = encode_rows(masked_rows)
    checksum = digest(data)
    terms = {
        "contract": contract_terms(contract),
        "masking": masking_terms(policy),
        "columns": list(effective),
    }

    staged: Path | None = None
    path: Path | None = None
    version = 0
    try:
        with transaction(connection, immediate=True):
            previous = connection.execute(
                "SELECT MAX(version) AS version FROM tenant_snapshots WHERE tenant=?", (tenant,)
            ).fetchone()
            version = 1 if previous["version"] is None else int(previous["version"]) + 1
            path = _snapshot_dir(export_dir, tenant) / f"v{version}.jsonl"
            staged = _stage_file(path, data)
            run_id = _record_run(
                connection,
                tenant=tenant,
                status="ready",
                reason="ready",
                detail="",
                as_of=as_of,
                requested_columns=requested,
                effective_columns=effective,
                row_count=len(masked_rows),
                snapshot_version=version,
                snapshot_path=str(path),
                checksum=checksum,
                **run_fields,
            )
            payload = {"rows": masked_rows, "row_count": len(masked_rows)}
            connection.execute(
                "INSERT INTO tenant_snapshots(tenant,version,run_id,contract_version,masking_version,columns,row_scope,rows,"
                "row_count,checksum,source_fingerprint,terms,as_of,created_at,path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    version,
                    run_id,
                    contract["version"],
                    contract["masking_version"],
                    encode(list(effective)),
                    encode(contract["row_scope"].as_dict()),
                    encode(payload),
                    len(masked_rows),
                    checksum,
                    fingerprint,
                    encode(terms),
                    as_of,
                    utc_now(),
                    str(path),
                ),
            )
            _commit_file(staged, path)
    except (OSError, sqlite3.Error) as error:
        if staged is not None:
            staged.unlink(missing_ok=True)
        return _blocked(
            connection,
            tenant,
            ContractError(f"快照写入失败: {error}", "write_failed"),
            as_of,
            requested,
            run_fields,
        )

    return {
        "status": "ready",
        "tenant": tenant,
        "snapshot_version": version,
        "contract_version": contract["version"],
        "masking_version": contract["masking_version"],
        "columns": list(effective),
        "row_count": len(masked_rows),
        "checksum": checksum,
        "path": str(path),
        "as_of": as_of,
        "terms": terms,
        "run_id": run_id,
    }


def read_snapshot(connection, tenant: str, version: int) -> dict:
    """Read a committed snapshot; contract expiry never removes history.

    The stored row set is authoritative and must match its checksum. The exported
    file is compared as well and reported through ``file_state`` so a replaced or
    deleted file is visible instead of silently disagreeing with the audit trail.
    """

    row = connection.execute(
        "SELECT * FROM tenant_snapshots WHERE tenant=? AND version=?", (tenant, int(version))
    ).fetchone()
    if not row:
        raise ContractError(f"租户 {tenant} 没有快照版本 {version}", "missing_snapshot")
    payload = decode(row["rows"])
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ContractError("快照内容损坏", "corrupt_snapshot")
    data = encode_rows(payload["rows"])
    if digest(data) != row["checksum"] or payload.get("row_count") != row["row_count"]:
        raise ContractError("快照校验失败: 内容与清单不一致", "corrupt_snapshot")
    file_state = "missing"
    try:
        on_disk = Path(row["path"]).read_bytes()
    except OSError:
        on_disk = None
    if on_disk is not None:
        file_state = "match" if digest(on_disk) == row["checksum"] else "mismatch"
    terms = decode(row["terms"]) if row["terms"] else {}
    contract = terms.get("contract")
    if contract is None:
        from .policy import contract_record

        contract = contract_terms(contract_record(connection, tenant, row["contract_version"]))
    masking = terms.get("masking")
    if masking is None:
        from .policy import get_masking_policy

        masking = masking_terms(get_masking_policy(connection, row["masking_version"]))
    return {
        "tenant": tenant,
        "snapshot_version": row["version"],
        "contract_version": row["contract_version"],
        "masking_version": row["masking_version"],
        "columns": decode(row["columns"]),
        "row_scope": decode(row["row_scope"]),
        "row_count": row["row_count"],
        "checksum": row["checksum"],
        "file_state": file_state,
        "as_of": row["as_of"],
        "created_at": row["created_at"],
        "path": row["path"],
        "rows": payload["rows"],
        # Terms are pinned at generation time; a later contract revision or
        # revocation must not rewrite what this snapshot was built from.
        "contract": contract,
        "masking": masking,
    }


def latest_snapshot(connection, tenant: str) -> dict | None:
    row = connection.execute(
        "SELECT version FROM tenant_snapshots WHERE tenant=? ORDER BY version DESC LIMIT 1", (tenant,)
    ).fetchone()
    return None if row is None else read_snapshot(connection, tenant, row["version"])


def run_status(connection, run_id: int) -> dict:
    row = connection.execute("SELECT * FROM export_runs WHERE id=?", (int(run_id),)).fetchone()
    if not row:
        raise ContractError(f"导出运行 {run_id} 不存在", "missing_run")
    result = dict(row)
    for key in ("requested_columns", "effective_columns", "row_scope"):
        result[key] = decode(result[key])
    return result


def blocked_runs(connection, tenant: str | None = None) -> list[dict]:
    if tenant is None:
        rows = connection.execute("SELECT * FROM export_runs WHERE status='blocked' ORDER BY id").fetchall()
    else:
        rows = connection.execute("SELECT * FROM export_runs WHERE status='blocked' AND tenant=? ORDER BY id", (tenant,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("requested_columns", "effective_columns", "row_scope"):
            item[key] = decode(item[key])
        result.append(item)
    return result


def explain_snapshot(connection, tenant: str, version: int) -> dict:
    snapshot = read_snapshot(connection, tenant, version)
    run = run_status(connection, connection.execute(
        "SELECT run_id FROM tenant_snapshots WHERE tenant=? AND version=?", (tenant, int(version))
    ).fetchone()["run_id"])
    from .policy import contract_record, contract_state

    current = contract_record(connection, tenant, snapshot["contract_version"])
    state = contract_state(current, snapshot["as_of"])
    return {
        "tenant": tenant,
        "snapshot_version": snapshot["snapshot_version"],
        "consumed_scope": {
            "columns": snapshot["columns"],
            "row_scope": snapshot["row_scope"],
            "row_count": snapshot["row_count"],
        },
        "generation_basis": {
            "contract": snapshot["contract"],
            "masking": snapshot["masking"],
            "as_of": snapshot["as_of"],
            "contract_state_at_snapshot": state,
            "contract_state_now": contract_state(current, utc_now()),
            "run": run,
        },
        "integrity": {
            "checksum": snapshot["checksum"],
            "file_state": snapshot["file_state"],
            "path": snapshot["path"],
            "created_at": snapshot["created_at"],
        },
    }


def compare_snapshots(connection, tenant: str, left: int, right: int) -> dict:
    """Show how the grant, masking rules and delivered rows changed between versions."""

    if int(left) >= int(right):
        raise ContractError("需要两个按时间先后排列的快照版本")
    first = read_snapshot(connection, tenant, left)
    second = read_snapshot(connection, tenant, right)

    first_columns = set(first["columns"])
    second_columns = set(second["columns"])
    first_mask = (first["masking"] or {}).get("rules", {})
    second_mask = (second["masking"] or {}).get("rules", {})
    mask_changes = {
        column: {"before": first_mask.get(column), "after": second_mask.get(column)}
        for column in sorted(set(first_mask) | set(second_mask))
        if first_mask.get(column) != second_mask.get(column)
    }
    first_scope = first["row_scope"]
    second_scope = second["row_scope"]

    first_contract = (first["contract"] or {}).get("contract_version")
    second_contract = (second["contract"] or {}).get("contract_version")
    return {
        "tenant": tenant,
        "before": {
            "snapshot_version": first["snapshot_version"],
            "contract_version": first_contract,
            "masking_version": first["masking_version"],
            "row_count": first["row_count"],
        },
        "after": {
            "snapshot_version": second["snapshot_version"],
            "contract_version": second_contract,
            "masking_version": second["masking_version"],
            "row_count": second["row_count"],
        },
        "columns": {
            "added": sorted(second_columns - first_columns),
            "removed": sorted(first_columns - second_columns),
        },
        "row_scope": {
            "changed": first_scope != second_scope,
            "before": first_scope,
            "after": second_scope,
        },
        "masking_rule_changes": mask_changes,
        "contract_changed": first_contract != second_contract,
        "row_count_delta": second["row_count"] - first["row_count"],
    }
