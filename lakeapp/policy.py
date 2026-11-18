from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .storage import decode, encode, normalize_timestamp, parse_timestamp, transaction, utc_now


class ContractError(ValueError):
    """A contract, masking policy or requested scope cannot be used."""

    def __init__(self, message: str, reason: str = "invalid_contract"):
        super().__init__(message)
        self.reason = reason


# Facts columns that are internal plumbing and can never be handed to a tenant.
CONTROL_COLUMNS = frozenset({"source", "payload", "source_batch", "version"})
SUPPORTED_RULES = frozenset({"redact", "hash", "partial", "email"})
REDACTED = "***REDACTED***"
PROTECTED = "***PROTECTED***"
TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
COLUMN_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _names(values: Iterable[str], field: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not COLUMN_PATTERN.match(value):
            raise ContractError(f"{field}必须是由字母、数字、下划线、点或短横线组成的非空字符串")
        if value not in result:
            result.append(value)
    return tuple(sorted(result))


def _time(value: Any, field: str) -> str:
    try:
        return normalize_timestamp(value)
    except (TypeError, ValueError) as error:
        raise ContractError(f"{field}不是有效时间") from error


@dataclass(frozen=True)
class RowScope:
    """Row filter resolved against the shared facts table for one contract.

    An unset condition means "not constrained by this condition"; at least one
    condition is required so an omitted filter can never silently mean all rows.
    """

    sources: tuple[str, ...] | None = None
    business_keys: tuple[str, ...] | None = None
    from_time: str | None = None
    to_time: str | None = None
    where: tuple[tuple[str, Any], ...] = ()
    everything: bool = False

    def as_dict(self) -> dict:
        if self.everything:
            return {"all": True}
        return {
            "sources": list(self.sources) if self.sources is not None else None,
            "business_keys": list(self.business_keys) if self.business_keys is not None else None,
            "from": self.from_time,
            "to": self.to_time,
            "where": {key: value for key, value in self.where},
        }

    def matches(self, row: Mapping[str, Any]) -> bool:
        if self.everything:
            return True
        if self.sources is not None and row.get("source") not in self.sources:
            return False
        if self.business_keys is not None and row.get("business_key") not in self.business_keys:
            return False
        if self.from_time is not None:
            occurred_at = row.get("occurred_at")
            if occurred_at is None or parse_timestamp(occurred_at) < parse_timestamp(self.from_time):
                return False
        if self.to_time is not None:
            occurred_at = row.get("occurred_at")
            if occurred_at is None or parse_timestamp(occurred_at) > parse_timestamp(self.to_time):
                return False
        for column, value in self.where:
            actual = row.get(column)
            if actual != value:
                return False
        return True


def normalize_row_scope(scope: Mapping[str, Any] | RowScope | None) -> RowScope:
    if scope is None:
        raise ContractError("缺少行范围", "missing_row_scope")
    if isinstance(scope, RowScope):
        normalized = scope
    elif isinstance(scope, Mapping):
        unknown = set(scope) - {"sources", "business_keys", "from", "to", "where", "all"}
        if unknown:
            raise ContractError("行范围含未知条件: " + ",".join(sorted(unknown)))
        if "all" in scope:
            if scope["all"] is not True:
                raise ContractError("行范围 all 只能为 true")
            if set(scope) - {"all"}:
                raise ContractError("行范围 all 不能与其他条件同时使用")
            return RowScope(everything=True)

        def names(key: str) -> tuple[str, ...] | None:
            value = scope.get(key)
            if value is None:
                return None
            if not isinstance(value, (list, tuple)):
                raise ContractError(f"行范围 {key} 必须是数组")
            return _names(value, f"行范围 {key}")

        where_value = scope.get("where") or {}
        if not isinstance(where_value, Mapping):
            raise ContractError("行范围 where 必须是对象")
        where: list[tuple[str, Any]] = []
        for column, value in sorted(where_value.items()):
            if not isinstance(column, str) or not COLUMN_PATTERN.match(column):
                raise ContractError("行范围 where 列名无效")
            if column in CONTROL_COLUMNS:
                raise ContractError(f"行范围不能以控制列 {column} 作为条件")
            if isinstance(value, (dict, list, tuple, set)):
                raise ContractError(f"行范围 where 列 {column} 的值必须是标量")
            where.append((column, value))
        normalized = RowScope(
            sources=names("sources"),
            business_keys=names("business_keys"),
            from_time=None if scope.get("from") is None else _time(scope["from"], "行范围 from"),
            to_time=None if scope.get("to") is None else _time(scope["to"], "行范围 to"),
            where=tuple(where),
        )
    else:
        raise ContractError("行范围必须是对象")

    if not normalized.everything and all(
        value is None
        for value in (normalized.sources, normalized.business_keys, normalized.from_time, normalized.to_time)
    ) and not normalized.where:
        raise ContractError("行范围至少需要一个条件；确需整表请显式写 {\"all\": true}")
    if normalized.from_time and normalized.to_time and parse_timestamp(normalized.from_time) > parse_timestamp(
        normalized.to_time
    ):
        raise ContractError("行范围起始时间晚于结束时间")
    return normalized


def register_masking_policy(connection, version: int, rules: Mapping[str, Any], protected: Iterable[str] = ()) -> None:
    """Register an immutable masking policy version.

    ``rules`` maps a fact column to a mask kind. ``protected`` names columns that
    must be hidden even without an explicit kind. Once written a version cannot
    change: contracts and snapshots pointing at it stay explainable.
    """

    version = int(version)
    if version <= 0:
        raise ContractError("脱敏规则版本必须是正整数")
    if not isinstance(rules, Mapping):
        raise ContractError("脱敏规则必须是对象")
    normalized_rules: dict[str, str] = {}
    for column, rule in rules.items():
        if not isinstance(column, str) or not COLUMN_PATTERN.match(column):
            raise ContractError("脱敏规则列名无效")
        if column in CONTROL_COLUMNS:
            raise ContractError(f"不能对固定控制列 {column} 设置脱敏规则")
        if rule not in SUPPORTED_RULES:
            raise ContractError(f"列 {column} 的脱敏规则不受支持: {rule}")
        normalized_rules[column] = rule
    normalized_rules = dict(sorted(normalized_rules.items()))
    protected_columns = _names(protected, "受保护列")
    overlap = set(normalized_rules) & set(protected_columns)
    if overlap:
        raise ContractError("列不能同时出现在脱敏规则和受保护列中: " + ",".join(sorted(overlap)))
    control = (set(normalized_rules) | set(protected_columns)) & CONTROL_COLUMNS
    if control:
        raise ContractError("不能把固定控制列设为脱敏或受保护列: " + ",".join(sorted(control)))

    rules_payload = encode(normalized_rules)
    protected_payload = encode(list(protected_columns))
    with transaction(connection, immediate=True):
        existing = connection.execute("SELECT rules,protected FROM masking_policies WHERE version=?", (version,)).fetchone()
        if existing:
            if (existing["rules"], existing["protected"]) != (rules_payload, protected_payload):
                raise ContractError(f"脱敏规则版本 {version} 已存在且内容不同")
            return
        connection.execute(
            "INSERT INTO masking_policies(version,rules,protected,created_at) VALUES(?,?,?,?)",
            (version, rules_payload, protected_payload, utc_now()),
        )


def get_masking_policy(connection, version: int) -> dict:
    row = connection.execute("SELECT version,rules,protected FROM masking_policies WHERE version=?", (int(version),)).fetchone()
    if not row:
        raise ContractError(f"脱敏规则版本 {version} 未注册", "missing_masking_policy")
    return {
        "version": row["version"],
        "rules": decode(row["rules"]),
        "protected": set(decode(row["protected"])),
    }


def register_contract(
    connection,
    tenant: str,
    version: int,
    columns: Iterable[str],
    row_scope: Mapping[str, Any] | RowScope | None,
    masking_version: int,
    effective_at: str,
    expires_at: str | None = None,
    revoked_at: str | None = None,
) -> None:
    """Append one immutable contract version for a tenant."""

    if not isinstance(tenant, str) or not TENANT_PATTERN.match(tenant):
        raise ContractError("租户必须由字母、数字、下划线、点或短横线组成")
    version = int(version)
    if version <= 0:
        raise ContractError("合同版本必须是正整数")
    granted = _names(columns, "授权列")
    if not granted:
        raise ContractError("授权列不能为空")
    control = set(granted) & CONTROL_COLUMNS
    if control:
        raise ContractError("合同不能授予固定控制列: " + ",".join(sorted(control)))
    scope = normalize_row_scope(row_scope)
    masking_version = int(masking_version)
    get_masking_policy(connection, masking_version)
    effective = _time(effective_at, "生效时间")
    expires = None if expires_at is None else _time(expires_at, "失效时间")
    revoked = None if revoked_at is None else _time(revoked_at, "撤销时间")
    if expires is not None and parse_timestamp(expires) <= parse_timestamp(effective):
        raise ContractError("失效时间必须晚于生效时间")
    if revoked is not None and parse_timestamp(revoked) < parse_timestamp(effective):
        raise ContractError("撤销时间不能早于生效时间")

    values = (encode(list(granted)), encode(scope.as_dict()), masking_version, effective, expires, revoked)
    with transaction(connection, immediate=True):
        existing = connection.execute(
            "SELECT columns,row_scope,masking_version,effective_at,expires_at,revoked_at FROM contract_versions "
            "WHERE tenant=? AND version=?",
            (tenant, version),
        ).fetchone()
        if existing:
            current = tuple(
                existing[key]
                for key in ("columns", "row_scope", "masking_version", "effective_at", "expires_at", "revoked_at")
            )
            if current != values:
                raise ContractError(f"租户 {tenant} 的合同版本 {version} 已存在且内容不同")
            return
        prior = connection.execute(
            "SELECT MAX(version) AS version, MAX(effective_at) AS effective_at, MAX(masking_version) AS masking_version "
            "FROM contract_versions WHERE tenant=?",
            (tenant,),
        ).fetchone()
        if prior["version"] is not None:
            if version < int(prior["version"]):
                raise ContractError(f"合同版本 {version} 早于已有版本 {prior['version']}，不能倒序追加")
            if parse_timestamp(effective) < parse_timestamp(prior["effective_at"]):
                raise ContractError("新合同版本的生效时间早于已有版本")
            # A renewal must not silently weaken masking; tightening is fine.
            if masking_version < int(prior["masking_version"]):
                raise ContractError(
                    f"新合同版本的脱敏规则版本 {masking_version} 早于已有版本的 {prior['masking_version']}"
                )
        connection.execute(
            "INSERT INTO contract_versions(tenant,version,columns,row_scope,masking_version,effective_at,expires_at,revoked_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (tenant, version, *values, utc_now()),
        )


def revoke_contract(connection, tenant: str, version: int, revoked_at: str | None = None) -> str:
    """Stop new reads under a contract version without touching old snapshots."""

    moment = _time(revoked_at, "撤销时间") if revoked_at is not None else utc_now()
    with transaction(connection, immediate=True):
        row = connection.execute(
            "SELECT revoked_at,effective_at FROM contract_versions WHERE tenant=? AND version=?", (tenant, int(version))
        ).fetchone()
        if not row:
            raise ContractError(f"租户 {tenant} 没有合同版本 {version}", "missing_contract")
        if parse_timestamp(moment) < parse_timestamp(row["effective_at"]):
            raise ContractError("撤销时间不能早于生效时间")
        if row["revoked_at"] is not None:
            if parse_timestamp(row["revoked_at"]) <= parse_timestamp(moment):
                return row["revoked_at"]
            raise ContractError("合同已经撤销，且已有更早的撤销时间")
        connection.execute("UPDATE contract_versions SET revoked_at=? WHERE tenant=? AND version=?", (moment, tenant, int(version)))
    return moment


def contract_record(connection, tenant: str, version: int) -> dict:
    row = connection.execute("SELECT * FROM contract_versions WHERE tenant=? AND version=?", (tenant, int(version))).fetchone()
    if not row:
        raise ContractError(f"租户 {tenant} 没有合同版本 {version}", "missing_contract")
    return {
        "tenant": row["tenant"],
        "version": row["version"],
        "columns": tuple(decode(row["columns"])),
        "row_scope": normalize_row_scope(decode(row["row_scope"])),
        "masking_version": row["masking_version"],
        "effective_at": row["effective_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "created_at": row["created_at"],
    }


def contract_state(contract: Mapping[str, Any], as_of: str) -> str:
    """Return ``pending``, ``active``, ``expired`` or ``revoked`` for an instant."""

    moment = parse_timestamp(as_of)
    if contract["revoked_at"] is not None and moment >= parse_timestamp(contract["revoked_at"]):
        return "revoked"
    if parse_timestamp(contract["effective_at"]) > moment:
        return "pending"
    if contract["expires_at"] is not None and moment >= parse_timestamp(contract["expires_at"]):
        return "expired"
    return "active"


def resolve_contract(connection, tenant: str, as_of: str, version: int | None = None) -> dict:
    """Resolve the contract governing reads for ``tenant`` at ``as_of``.

    The version applicable at ``as_of`` is the newest one already in effect.
    Its state decides whether reads continue: once it is expired or revoked the
    tenant is blocked instead of silently falling back to an older grant. An
    older version is therefore never reused as authorization; passing ``version``
    only confirms the applicable one and cannot pin a superseded grant.
    """

    moment = _time(as_of, "核对时间")
    instant = parse_timestamp(moment)
    rows = connection.execute("SELECT version FROM contract_versions WHERE tenant=? ORDER BY version DESC", (tenant,)).fetchall()
    if not rows:
        raise ContractError(f"租户 {tenant} 没有任何合同版本", "missing_contract")
    records = [contract_record(connection, tenant, row["version"]) for row in rows]
    applicable = [item for item in records if parse_timestamp(item["effective_at"]) <= instant]
    if not applicable:
        raise ContractError(f"租户 {tenant} 在 {moment} 没有已生效的合同", "contract_inactive")
    current = applicable[0]
    if version is not None and int(version) != current["version"]:
        raise ContractError(
            f"合同版本 {version} 已被版本 {current['version']} 取代，不能用于新的导出", "contract_inactive"
        )
    state = contract_state(current, moment)
    if state != "active":
        raise ContractError(f"租户 {tenant} 的合同版本 {current['version']} 在 {moment} 不生效: {state}", "contract_inactive")
    return current


def projection(contract: Mapping[str, Any], requested: Iterable[str] | None) -> tuple[str, ...]:
    """Resolve requested columns, which must be a subset of the contract grant."""

    if requested is None:
        return tuple(sorted(contract["columns"]))
    wanted = _names(requested, "请求列")
    if not wanted:
        raise ContractError("请求列不能为空", "unauthorized_columns")
    granted = set(contract["columns"])
    denied = [column for column in wanted if column not in granted]
    if denied:
        raise ContractError("请求列未获授权: " + ",".join(sorted(denied)), "unauthorized_columns")
    return wanted


def mask_value(column: str, rule: str, value: Any) -> Any:
    if value is None:
        return None
    if rule == "redact":
        return REDACTED
    if rule == "hash":
        from .storage import digest

        return digest(f"{column}:{encode(value)}".encode("utf-8"))
    if rule == "partial":
        text = str(value)
        return text[:1] + "***" if text else REDACTED
    if rule == "email":
        text = str(value)
        local, separator, domain = text.partition("@")
        if not separator or not local or not domain or "." not in domain:
            raise ContractError(f"列 {column} 不是可脱敏的邮件地址", "masking_failed")
        return f"{local[0]}***@{domain}"
    raise ContractError(f"列 {column} 的脱敏规则不受支持: {rule}", "masking_failed")


def apply_masking(columns: Iterable[str], row: Mapping[str, Any], policy: Mapping[str, Any]) -> dict:
    """Build the delivered row, applying the pinned masking policy."""

    rules = policy["rules"]
    protected = policy["protected"]
    masked: dict[str, Any] = {}
    for column in columns:
        value = row.get(column)
        if column in protected:
            masked[column] = None if value is None else PROTECTED
        elif column in rules:
            masked[column] = mask_value(column, rules[column], value)
        else:
            masked[column] = value
    return masked


def verify_masking(raw_rows: list[dict], masked_rows: list[dict], columns: Iterable[str], policy: Mapping[str, Any]) -> None:
    """Re-check delivered rows so a masking bug cannot silently ship raw values.

    The expected value is recomputed independently of the masking pass, so a
    column that fails to mask, or is changed without a masking rule, blocks the
    export instead of being delivered.
    """

    rules = policy["rules"]
    protected = policy["protected"]
    expected = set(columns)
    if len(raw_rows) != len(masked_rows):
        raise ContractError("脱敏前后行数不一致", "masking_failed")
    for raw, masked in zip(raw_rows, masked_rows):
        if set(masked) != expected:
            raise ContractError("脱敏结果包含未获授权的列", "unauthorized_columns")
        for column in masked:
            raw_value = raw.get(column)
            delivered = masked.get(column)
            if column in protected:
                expected_value = None if raw_value is None else PROTECTED
            elif column in rules:
                expected_value = mask_value(column, rules[column], raw_value)
            else:
                expected_value = raw_value
            if delivered != expected_value:
                raise ContractError(f"列 {column} 脱敏结果不符合规则", "masking_failed")


def row_identity(row: Mapping[str, Any]) -> str:
    from .storage import digest

    source = row.get("source")
    business_key = row.get("business_key")
    if not source or not business_key:
        raise ContractError("事实缺少来源或业务键，无法界定行范围")
    return digest(f"{source}\x1f{business_key}".encode("utf-8"))


def allowed_columns(requested: set[str], granted: set[str], strict: bool = False) -> set[str]:
    """Intersect requested and granted columns.

    Kept for callers that only need the intersection. Export never drops denied
    columns silently: it calls :func:`projection`, or passes ``strict=True`` here.
    """

    denied = set(requested) - set(granted)
    if strict and denied:
        raise ContractError("请求列未获授权: " + ",".join(sorted(denied)), "unauthorized_columns")
    return set(requested) & set(granted)


def access_state(
    contract_active: bool,
    masking_ok: bool,
    authorized: bool = True,
    columns_ok: bool = True,
) -> str:
    """Human-readable gate state, in the order that blocks delivery first."""

    if not authorized:
        return "缺少授权"
    if not contract_active:
        return "合同失效"
    if not columns_ok:
        return "越权列"
    if not masking_ok:
        return "脱敏阻断"
    return "可生成"


def contract_terms(contract: Mapping[str, Any]) -> dict:
    return {
        "tenant": contract["tenant"],
        "contract_version": contract["version"],
        "granted_columns": list(contract["columns"]),
        "row_scope": contract["row_scope"].as_dict(),
        "masking_version": contract["masking_version"],
        "effective_at": contract["effective_at"],
        "expires_at": contract["expires_at"],
        "revoked_at": contract["revoked_at"],
    }


def masking_terms(policy: Mapping[str, Any]) -> dict:
    return {
        "version": policy["version"],
        "rules": dict(policy["rules"]),
        "protected": sorted(policy["protected"]),
    }
