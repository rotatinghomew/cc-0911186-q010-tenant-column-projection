from __future__ import annotations

from .contract import Event
from .storage import encode, transaction


def apply_events(connection, events: list[Event]) -> int:
    changed = 0
    with transaction(connection):
        for event in events:
            if event.kind == "retract":
                connection.execute("DELETE FROM facts WHERE source=? AND business_key=?", (event.source, event.business_key))
            else:
                connection.execute(
                    "INSERT INTO facts(source,business_key,payload,source_batch,version) VALUES(?,?,?,?,1) "
                    "ON CONFLICT(source,business_key) DO UPDATE SET payload=excluded.payload,source_batch=excluded.source_batch,version=facts.version+1",
                    (event.source, event.business_key, encode({"value": event.value, "occurred_at": event.occurred_at}), event.batch_id),
                )
            changed += 1
    return changed


def facts(connection) -> list[dict]:
    return [dict(row) for row in connection.execute("SELECT source,business_key,payload,source_batch,version FROM facts ORDER BY source,business_key")]
