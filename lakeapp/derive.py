from __future__ import annotations

from .contract import Event
from .storage import decode, encode, transaction


def apply_events(connection, events: list[Event]) -> int:
    changed = 0
    with transaction(connection):
        for event in events:
            if event.kind == "retract":
                connection.execute("DELETE FROM facts WHERE source=? AND business_key=?", (event.source, event.business_key))
            else:
                existing = connection.execute(
                    "SELECT version FROM facts WHERE source=? AND business_key=?", (event.source, event.business_key)
                ).fetchone()
                version = 1 if existing is None else existing["version"] + 1
                payload = {"value": event.value, "occurred_at": event.occurred_at}
                payload.update(event.attribute_dict())
                connection.execute(
                    "INSERT INTO facts(source,business_key,payload,source_batch,version) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(source,business_key) DO UPDATE SET payload=excluded.payload,"
                    "source_batch=excluded.source_batch,version=excluded.version",
                    (event.source, event.business_key, encode(payload), event.batch_id, version),
                )
            changed += 1
    return changed


def _fact_row(row) -> dict:
    payload = decode(row["payload"])
    result = {
        "source": row["source"],
        "business_key": row["business_key"],
        "value": payload.get("value"),
        "occurred_at": payload.get("occurred_at"),
        "source_batch": row["source_batch"],
        "version": row["version"],
    }
    for column, value in payload.items():
        if column not in result:
            result[column] = value
    return result


def fact_rows(connection) -> list[dict]:
    """Current derived facts. ``facts`` is the public name for this view."""

    return [
        _fact_row(row)
        for row in connection.execute(
            "SELECT source,business_key,payload,source_batch,version FROM facts ORDER BY source,business_key"
        )
    ]


def facts(connection) -> list[dict]:
    return fact_rows(connection)
