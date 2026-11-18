import json
import tempfile
import unittest
from pathlib import Path

from lakeapp.derive import apply_events, facts
from lakeapp.ingest import progress, receive
from lakeapp.publish import publish, read_manifest
from lakeapp.storage import open_store


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.connection = open_store(Path(self.tempdir.name) / "data.db")

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def test_receive_is_idempotent_and_derives_fact(self):
        records = [{"business_key": "member-1", "kind": "upsert", "value": 3, "occurred_at": "2026-11-01T09:00:00"}]
        events = receive(self.connection, "enrollment", "batch-1", 1, records)
        self.assertEqual(apply_events(self.connection, events), 1)
        repeated = receive(self.connection, "enrollment", "batch-1", 1, records)
        self.assertEqual(apply_events(self.connection, repeated), 1)
        self.assertEqual(len(facts(self.connection)), 1)
        self.assertEqual(progress(self.connection)[0]["cursor"], 1)

    def test_retract_and_manifest_round_trip(self):
        records = [{"business_key": "member-1", "kind": "upsert", "value": 3, "occurred_at": "2026-11-01T09:00:00"}]
        apply_events(self.connection, receive(self.connection, "attendance", "batch-1", 1, records))
        retract = [{"business_key": "member-1", "kind": "retract", "value": 0, "occurred_at": "2026-11-02T09:00:00"}]
        apply_events(self.connection, receive(self.connection, "attendance", "batch-2", 2, retract))
        publish(self.connection, "daily", 1, facts(self.connection))
        self.assertEqual(read_manifest(self.connection, "daily", 1)["row_count"], 0)


if __name__ == "__main__":
    unittest.main()
