from __future__ import annotations


def allowed_columns(requested: set[str], granted: set[str]) -> set[str]:
    return requested & granted


def access_state(contract_active: bool, masking_ok: bool) -> str:
    if not contract_active:
        return "合同失效"
    if not masking_ok:
        return "脱敏阻断"
    return "可生成"
