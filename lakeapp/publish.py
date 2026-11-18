from __future__ import annotations

from .storage import decode, encode, transaction


def publish(connection, name: str, version: int, rows: list[dict]) -> None:
    payload = {"rows": rows, "row_count": len(rows)}
    with transaction(connection):
        connection.execute(
            "INSERT INTO manifests(name,version,payload,status) VALUES(?,?,?,?)",
            (name, version, encode(payload), "ready"),
        )


def read_manifest(connection, name: str, version: int) -> dict:
    row = connection.execute("SELECT payload,status FROM manifests WHERE name=? AND version=?", (name, version)).fetchone()
    if not row or row["status"] != "ready":
        raise KeyError("版本不可读")
    return decode(row["payload"])
