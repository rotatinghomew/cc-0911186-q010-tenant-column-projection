from __future__ import annotations

from .contract import Event, parse_event
from .storage import encode, transaction


def receive(connection, source: str, batch_id: str, cursor: int, records: list[dict]) -> list[Event]:
    existing = connection.execute(
        "SELECT payload FROM raw_batches WHERE source=? AND batch_id=?", (source, batch_id)
    ).fetchone()
    payload = {"cursor": cursor, "records": records}
    if existing:
        if existing["payload"] != encode(payload):
            raise ValueError("同一批次内容冲突")
        return [parse_event(source, batch_id, cursor, item) for item in records]
    with transaction(connection):
        connection.execute(
            "INSERT INTO raw_batches(source,batch_id,cursor,payload,received_at) VALUES(?,?,?,?,datetime('now'))",
            (source, batch_id, cursor, encode(payload)),
        )
        connection.execute(
            "INSERT INTO source_progress(source,cursor) VALUES(?,?) ON CONFLICT(source) DO UPDATE SET cursor=MAX(cursor,excluded.cursor)",
            (source, cursor),
        )
    return [parse_event(source, batch_id, cursor, item) for item in records]


def progress(connection) -> list[dict]:
    return [dict(row) for row in connection.execute("SELECT source,cursor FROM source_progress ORDER BY source")]
