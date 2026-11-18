from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .policy import CONTROL_COLUMNS


@dataclass(frozen=True)
class Event:
    source: str
    batch_id: str
    cursor: int
    business_key: str
    kind: str
    value: float
    occurred_at: str
    attributes: tuple[tuple[str, Any], ...] = field(default_factory=tuple)

    def attribute_dict(self) -> dict[str, Any]:
        return dict(self.attributes)


def parse_event(source: str, batch_id: str, cursor: int, raw: dict) -> Event:
    required = {"business_key", "kind", "value", "occurred_at"}
    missing = required - raw.keys()
    if missing:
        raise ValueError("缺少字段: " + ",".join(sorted(missing)))
    if not isinstance(raw["business_key"], str) or not raw["business_key"]:
        raise ValueError("业务键不能为空")
    if raw["kind"] not in {"upsert", "retract"}:
        raise ValueError("事件类型不受支持")
    attributes: list[tuple[str, Any]] = []
    for column, value in sorted(raw.items()):
        if column in required:
            continue
        if column in CONTROL_COLUMNS:
            raise ValueError(f"来源记录不能覆盖控制列: {column}")
        attributes.append((column, value))
    return Event(
        source,
        batch_id,
        int(cursor),
        raw["business_key"],
        raw["kind"],
        float(raw["value"]),
        str(raw["occurred_at"]),
        tuple(attributes),
    )
