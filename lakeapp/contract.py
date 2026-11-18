from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    source: str
    batch_id: str
    cursor: int
    business_key: str
    kind: str
    value: float
    occurred_at: str


def parse_event(source: str, batch_id: str, cursor: int, raw: dict) -> Event:
    required = {"business_key", "kind", "value", "occurred_at"}
    missing = required - raw.keys()
    if missing:
        raise ValueError("缺少字段: " + ",".join(sorted(missing)))
    if not isinstance(raw["business_key"], str) or not raw["business_key"]:
        raise ValueError("业务键不能为空")
    if raw["kind"] not in {"upsert", "retract"}:
        raise ValueError("事件类型不受支持")
    return Event(source, batch_id, int(cursor), raw["business_key"], raw["kind"], float(raw["value"]), str(raw["occurred_at"]))
