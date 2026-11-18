from __future__ import annotations

from .policy import contract_state, contract_terms, masking_terms
from .publish import latest_snapshot, read_snapshot
from .storage import utc_now


def snapshot_scope(tenant: str, columns: set[str], rows: int, policy_version: int) -> dict:
    return {"tenant": tenant, "columns": sorted(columns), "rows": rows, "policy_version": policy_version}


def delivery_scope(scope: dict) -> dict:
    """Canonical description of what a tenant was actually delivered."""

    return {
        "tenant": scope["tenant"],
        "snapshot_version": scope["snapshot_version"],
        "columns": scope["columns"],
        "row_scope": scope["row_scope"],
        "row_count": scope["row_count"],
        "contract_version": scope["contract_version"],
        "masking_version": scope["masking_version"],
        "as_of": scope["as_of"],
    }


def delivery_basis(scope: dict) -> dict:
    return {
        "contract": scope["contract"],
        "masking": scope["masking"],
        "checksum": scope["checksum"],
        "path": scope["path"],
        "created_at": scope["created_at"],
    }


def version_scope(connection, tenant: str, version: int) -> dict:
    """Everything a reviewer needs to confirm one snapshot, even after revocation."""

    snapshot = read_snapshot(connection, tenant, version)
    from .policy import contract_record

    contract = contract_record(connection, tenant, snapshot["contract_version"])
    return {
        "delivered": delivery_scope(snapshot),
        "basis": delivery_basis(snapshot),
        "contract_state_at_snapshot": contract_state(contract, snapshot["as_of"]),
        "contract_state_now": contract_state(contract, utc_now()),
        "readable": True,
    }


def contract_history(connection, tenant: str) -> list[dict]:
    rows = connection.execute("SELECT * FROM contract_versions WHERE tenant=? ORDER BY version", (tenant,)).fetchall()
    from .policy import contract_record

    return [
        {
            "contract": contract_terms(contract_record(connection, tenant, row["version"])),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def masking_history(connection) -> list[dict]:
    from .policy import get_masking_policy

    rows = connection.execute("SELECT version,created_at FROM masking_policies ORDER BY version").fetchall()
    return [{"created_at": row["created_at"], "policy": masking_terms(get_masking_policy(connection, row["version"]))} for row in rows]


def tenant_overview(connection, tenant: str) -> dict:
    """Latest delivery plus any currently blocked generation."""

    from .publish import blocked_runs

    latest = latest_snapshot(connection, tenant)
    blocking = blocked_runs(connection, tenant)
    return {
        "tenant": tenant,
        "latest_delivery": None if latest is None else delivery_scope(latest),
        "blocked_runs": [
            {
                "run_id": run["id"],
                "status": run["status"],
                "reason": run["reason"],
                "detail": run["detail"],
                "requested_columns": run["requested_columns"],
                "effective_columns": run["effective_columns"],
                "as_of": run["as_of"],
                "created_at": run["created_at"],
            }
            for run in blocking
        ],
    }


def scope_for_columns(connection, tenant: str, columns: set[str], rows: int, policy_version: int, as_of: str | None = None) -> dict:
    """Projection preview for a hypothetical scope without writing a snapshot."""

    from .policy import ContractError, resolve_contract

    contract = resolve_contract(connection, tenant, utc_now() if as_of is None else as_of)
    granted = set(contract["columns"])
    missing = sorted(set(columns) - granted)
    if missing:
        raise ContractError("请求列未获授权: " + ",".join(missing), "unauthorized_columns")
    return snapshot_scope(tenant, set(columns) & granted, rows, policy_version)
