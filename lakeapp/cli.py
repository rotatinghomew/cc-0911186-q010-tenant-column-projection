from __future__ import annotations

import argparse
import json

from .derive import apply_events, facts
from .ingest import progress, receive
from .policy import (
    ContractError,
    register_contract,
    register_masking_policy,
    revoke_contract,
)
from .publish import (
    blocked_runs,
    compare_snapshots,
    explain_snapshot,
    export_tenant,
    publish,
    read_manifest,
    run_status,
)
from .query import contract_history, masking_history, tenant_overview, version_scope
from .storage import open_store


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本地分层数据服务")
    parser.add_argument("database")
    parser.add_argument(
        "action",
        choices=[
            "receive",
            "facts",
            "progress",
            "publish",
            "read",
            "masking",
            "maskings",
            "contract",
            "contracts",
            "revoke",
            "export",
            "audit",
            "run",
            "explain",
            "scope",
            "diff",
            "tenant",
        ],
    )
    parser.add_argument("payload", nargs="?")
    args = parser.parse_args(argv)
    data = json.loads(args.payload or "{}")
    connection = open_store(args.database)
    try:
        if args.action == "receive":
            events = receive(connection, data["source"], data["batch_id"], data["cursor"], data["records"])
            print(_json({"received": len(events), "changed": apply_events(connection, events)}))
        elif args.action == "facts":
            print(_json(facts(connection)))
        elif args.action == "progress":
            print(_json(progress(connection)))
        elif args.action == "publish":
            publish(connection, data["name"], data["version"], facts(connection))
            print("ok")
        elif args.action == "read":
            print(_json(read_manifest(connection, data["name"], data["version"])))
        elif args.action == "masking":
            register_masking_policy(connection, data["version"], data.get("rules", {}), data.get("protected", []))
            print("ok")
        elif args.action == "contract":
            register_contract(
                connection,
                data["tenant"],
                data["version"],
                data["columns"],
                data["row_scope"],
                data["masking_version"],
                data["effective_at"],
                data.get("expires_at"),
                data.get("revoked_at"),
            )
            print("ok")
        elif args.action == "revoke":
            print(_json({"revoked_at": revoke_contract(connection, data["tenant"], data["version"], data.get("revoked_at"))}))
        elif args.action == "export":
            result = export_tenant(
                connection,
                data["tenant"],
                data["export_dir"],
                data.get("as_of"),
                data.get("columns"),
                data.get("contract_version"),
            )
            print(_json(result))
            if result["status"] != "ready":
                return 1
        elif args.action == "audit":
            print(_json(blocked_runs(connection, data.get("tenant"))))
        elif args.action == "run":
            print(_json(run_status(connection, data["run_id"])))
        elif args.action == "explain":
            print(_json(explain_snapshot(connection, data["tenant"], data["version"])))
        elif args.action == "scope":
            print(_json(version_scope(connection, data["tenant"], data["version"])))
        elif args.action == "diff":
            print(_json(compare_snapshots(connection, data["tenant"], data["left"], data["right"])))
        elif args.action == "contracts":
            print(_json(contract_history(connection, data["tenant"])))
        elif args.action == "maskings":
            print(_json(masking_history(connection)))
        else:
            print(_json(tenant_overview(connection, data["tenant"])))
    except ContractError as error:
        print(_json({"status": "blocked", "reason": error.reason, "detail": str(error)}))
        return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
