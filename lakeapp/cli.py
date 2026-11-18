from __future__ import annotations

import argparse
import json

from .derive import apply_events, facts
from .ingest import progress, receive
from .publish import publish, read_manifest
from .storage import open_store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本地分层数据服务")
    parser.add_argument("database")
    parser.add_argument("action", choices=["receive", "facts", "progress", "publish", "read"])
    parser.add_argument("payload", nargs="?")
    args = parser.parse_args(argv)
    connection = open_store(args.database)
    try:
        if args.action == "receive":
            data = json.loads(args.payload or "{}")
            events = receive(connection, data["source"], data["batch_id"], data["cursor"], data["records"])
            print(json.dumps({"received": len(events), "changed": apply_events(connection, events)}, ensure_ascii=False))
        elif args.action == "facts":
            print(json.dumps(facts(connection), ensure_ascii=False))
        elif args.action == "progress":
            print(json.dumps(progress(connection), ensure_ascii=False))
        elif args.action == "publish":
            data = json.loads(args.payload or "{}")
            publish(connection, data["name"], data["version"], facts(connection))
            print("ok")
        else:
            data = json.loads(args.payload or "{}")
            print(json.dumps(read_manifest(connection, data["name"], data["version"]), ensure_ascii=False))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
