import json
import tempfile
import unittest
from pathlib import Path

from lakeapp.derive import apply_events, facts
from lakeapp.ingest import receive
from lakeapp.policy import (
    ContractError,
    contract_record,
    get_masking_policy,
    register_contract,
    register_masking_policy,
    revoke_contract,
    resolve_contract,
)
from lakeapp.publish import (
    blocked_runs,
    compare_snapshots,
    explain_snapshot,
    export_tenant,
    read_snapshot,
)
from lakeapp.query import contract_history, tenant_overview, version_scope
from lakeapp.storage import open_store


RECORDS = [
    {
        "business_key": "member-1",
        "kind": "upsert",
        "value": 3,
        "occurred_at": "2026-11-01T09:00:00",
        "email": "alice@example.com",
        "display_name": "Alice",
        "cohort": "A",
    },
    {
        "business_key": "member-2",
        "kind": "upsert",
        "value": 5,
        "occurred_at": "2026-11-05T09:00:00",
        "email": "bob@example.com",
        "display_name": "Bob",
        "cohort": "B",
    },
]


class TenantExportTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.connection = open_store(self.root / "data.db")
        apply_events(self.connection, receive(self.connection, "enrollment", "batch-1", 1, RECORDS))
        register_masking_policy(
            self.connection,
            1,
            {"email": "email", "display_name": "partial"},
            protected=["cohort"],
        )

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def grant(self, tenant="partner-a", version=1, columns=None, scope=None, effective="2026-11-01T00:00:00", masking_version=1, **kwargs):
        register_contract(
            self.connection,
            tenant,
            version,
            columns or ["business_key", "value", "email"],
            scope or {"sources": ["enrollment"]},
            masking_version,
            effective,
            **kwargs,
        )

    def export(self, tenant="partner-a", **kwargs):
        kwargs.setdefault("as_of", "2026-11-10T00:00:00")
        return export_tenant(self.connection, tenant, self.root / "exports", **kwargs)

    def test_delivered_file_contains_only_granted_masked_columns(self):
        self.grant()
        result = self.export()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["columns"], ["business_key", "email", "value"])

        lines = Path(result["path"]).read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(set(row), {"business_key", "email", "value"})
            self.assertNotIn("display_name", row)
            self.assertNotIn("cohort", row)
        self.assertEqual(rows[0]["email"], "a***@example.com")

        stored = read_snapshot(self.connection, "partner-a", 1)
        self.assertEqual(stored["rows"], rows)
        self.assertEqual(stored["checksum"], result["checksum"])

    def test_contract_revision_changes_later_snapshot_but_not_history(self):
        self.grant()
        first = self.export()
        register_masking_policy(self.connection, 2, {"email": "redact"})
        self.grant(version=2, columns=["business_key", "email", "display_name"], scope={"business_keys": ["member-2"]}, effective="2026-11-11T00:00:00", masking_version=2)

        second = self.export(as_of="2026-11-12T00:00:00")
        self.assertEqual(second["contract_version"], 2)
        self.assertEqual(second["masking_version"], 2)
        self.assertEqual(second["columns"], ["business_key", "display_name", "email"])
        self.assertEqual(second["row_count"], 1)
        delivered = Path(second["path"]).read_text(encoding="utf-8")
        self.assertIn("***REDACTED***", delivered)
        self.assertNotIn("@example.com", delivered)
        self.assertIn("Bob", delivered)

        original = read_snapshot(self.connection, "partner-a", first["snapshot_version"])
        self.assertEqual(original["masking_version"], 1)
        self.assertEqual(original["row_count"], 2)
        self.assertEqual(original["rows"][0]["email"], "a***@example.com")

        diff = compare_snapshots(self.connection, "partner-a", first["snapshot_version"], second["snapshot_version"])
        self.assertTrue(diff["contract_changed"])
        self.assertEqual(diff["masking_rule_changes"]["email"], {"before": "email", "after": "redact"})
        self.assertEqual(diff["columns"]["added"], ["display_name"])
        self.assertEqual(diff["row_count_delta"], -1)

    def test_unauthorized_column_is_blocked_not_silently_dropped(self):
        self.grant()
        result = self.export(columns=["business_key", "cohort"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "unauthorized_columns")
        self.assertNotIn("path", result)
        self.assertEqual(list((self.root / "exports").rglob("*.jsonl")), [])

        runs = blocked_runs(self.connection, "partner-a")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["requested_columns"], ["business_key", "cohort"])
        self.assertIsNone(runs[0]["snapshot_path"])

    def test_missing_contract_is_blocked_and_queryable(self):
        result = self.export(tenant="partner-x")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "missing_contract")
        overview = tenant_overview(self.connection, "partner-x")
        self.assertIsNone(overview["latest_delivery"])
        self.assertEqual(overview["blocked_runs"][0]["reason"], "missing_contract")

    def test_revoked_contract_blocks_new_reads_but_keeps_snapshots(self):
        self.grant()
        first = self.export()
        revoke_contract(self.connection, "partner-a", 1, "2026-11-11T00:00:00")

        blocked = self.export(as_of="2026-11-12T00:00:00")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "contract_inactive")

        scope = version_scope(self.connection, "partner-a", first["snapshot_version"])
        self.assertEqual(scope["delivered"]["row_count"], 2)
        self.assertEqual(scope["contract_state_at_snapshot"], "active")
        self.assertEqual(scope["contract_state_now"], "revoked")
        self.assertEqual(scope["basis"]["contract"]["contract_version"], 1)

    def test_ruleset_change_does_not_reinterpret_old_snapshot(self):
        self.grant()
        first = self.export()
        register_masking_policy(self.connection, 2, {"email": "redact", "display_name": "redact"})
        still = read_snapshot(self.connection, "partner-a", first["snapshot_version"])
        self.assertEqual(still["masking_version"], 1)
        self.assertEqual(still["masking"]["rules"], {"display_name": "partial", "email": "email"})
        explained = explain_snapshot(self.connection, "partner-a", first["snapshot_version"])
        self.assertEqual(explained["generation_basis"]["masking"]["version"], 1)
        self.assertEqual(explained["consumed_scope"]["columns"], ["business_key", "email", "value"])

    def test_masking_failure_blocks_with_reason(self):
        register_masking_policy(self.connection, 3, {"email": "email"})
        self.connection.execute("UPDATE facts SET payload=? WHERE business_key='member-1'", ('{"occurred_at":"2026-11-01T09:00:00","value":3,"email":"not-an-email"}',))
        register_contract(self.connection, "partner-bad", 1, ["business_key", "email"], {"all": True}, 3, "2026-11-01T00:00:00")
        result = self.export(tenant="partner-bad")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "masking_failed")
        self.assertEqual(list((self.root / "exports").rglob("*.jsonl")), [])

    def test_tenant_cannot_reuse_another_tenants_snapshot_or_cache(self):
        self.grant(tenant="partner-a", columns=["business_key", "value"])
        self.grant(tenant="partner-b", columns=["business_key", "email"], scope={"business_keys": ["member-2"]})
        result_a = self.export(tenant="partner-a")
        result_b = self.export(tenant="partner-b")

        self.assertNotEqual(result_a["path"], result_b["path"])
        self.assertEqual(result_a["snapshot_version"], 1)
        self.assertEqual(result_b["snapshot_version"], 1)
        self.assertEqual(result_a["columns"], ["business_key", "value"])
        self.assertEqual(result_b["columns"], ["business_key", "email"])
        self.assertEqual(result_b["row_count"], 1)
        self.assertIn("partner-a", result_a["path"])
        self.assertIn("partner-b", result_b["path"])

        snapshot_a = read_snapshot(self.connection, "partner-a", 1)
        snapshot_b = read_snapshot(self.connection, "partner-b", 1)
        self.assertEqual(snapshot_a["columns"], ["business_key", "value"])
        self.assertNotIn("email", snapshot_a["rows"][0])
        self.assertEqual(snapshot_b["rows"][0]["email"], "b***@example.com")

    def test_row_scope_limits_delivered_rows(self):
        self.grant(scope={"sources": ["enrollment"], "business_keys": ["member-1"]})
        result = self.export()
        self.assertEqual(result["row_count"], 1)
        rows = read_snapshot(self.connection, "partner-a", 1)["rows"]
        self.assertEqual(rows[0]["business_key"], "member-1")

    def test_superseded_contract_cannot_be_selected_for_new_export(self):
        self.grant()
        self.grant(version=2, effective="2026-11-11T00:00:00")
        with self.assertRaises(ContractError):
            resolve_contract(self.connection, "partner-a", "2026-11-12T00:00:00", version=1)
        blocked = self.export(as_of="2026-11-12T00:00:00", contract_version=1)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "contract_inactive")

    def test_contract_history_is_queryable(self):
        self.grant()
        self.grant(version=2, effective="2026-11-11T00:00:00")
        history = contract_history(self.connection, "partner-a")
        self.assertEqual([item["contract"]["contract_version"] for item in history], [1, 2])
        self.assertEqual(history[1]["contract"]["granted_columns"], ["business_key", "email", "value"])

    def test_contract_and_masking_versions_are_immutable(self):
        self.grant()
        with self.assertRaises(ContractError):
            self.grant(columns=["business_key", "value"])
        with self.assertRaises(ContractError):
            register_masking_policy(self.connection, 1, {"email": "redact"})
        self.assertEqual(get_masking_policy(self.connection, 1)["rules"]["email"], "email")
        self.assertEqual(contract_record(self.connection, "partner-a", 1)["columns"], ("business_key", "email", "value"))

    def test_protected_column_is_never_delivered_raw(self):
        self.connection.execute(
            "UPDATE facts SET payload=? WHERE business_key='member-2'",
            ('{"occurred_at":"2026-11-05T09:00:00","value":5,"email":"bob@example.com","display_name":"Bob","cohort":"B"}',),
        )
        self.grant(columns=["business_key", "cohort"])
        result = self.export()
        self.assertEqual(result["status"], "ready")
        rows = read_snapshot(self.connection, "partner-a", 1)["rows"]
        self.assertEqual(rows[0]["cohort"], "***PROTECTED***")

    def test_renewal_cannot_weaken_masking_version(self):
        register_masking_policy(self.connection, 2, {"email": "redact"})
        self.grant(version=1, masking_version=2)
        with self.assertRaises(ContractError):
            self.grant(version=2, masking_version=1, effective="2026-11-11T00:00:00")

    def test_snapshot_detects_replaced_file(self):
        self.grant()
        result = self.export()
        Path(result["path"]).write_text('{"business_key":"member-1","email":"alice@example.com","value":3}\n', encoding="utf-8")
        snapshot = read_snapshot(self.connection, "partner-a", 1)
        self.assertEqual(snapshot["file_state"], "mismatch")
        self.assertEqual(snapshot["rows"][0]["email"], "a***@example.com")
        explained = explain_snapshot(self.connection, "partner-a", 1)
        self.assertEqual(explained["integrity"]["checksum"], result["checksum"])


if __name__ == "__main__":
    unittest.main()
