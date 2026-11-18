from __future__ import annotations


def snapshot_scope(tenant: str, columns: set[str], rows: int, policy_version: int) -> dict:
    return {"tenant": tenant, "columns": sorted(columns), "rows": rows, "policy_version": policy_version}
