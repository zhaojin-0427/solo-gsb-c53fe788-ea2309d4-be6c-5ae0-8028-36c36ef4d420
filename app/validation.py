"""入参校验：3～9 个唯一编号旁路组，Voc≤100V、Isc≤30A 的正数（至多两位小数）。"""

import re
from decimal import Decimal, InvalidOperation

from .model import Group, MAX_STRINGS

MIN_GROUPS, MAX_GROUPS = 3, 9
MAX_ID_LEN = 32
MAX_DECIMAL = Decimal("100")
MAX_CURRENT = Decimal("30")
# 允许整数或两位以内小数，拒绝科学计数法
_NUM_RE = re.compile(r"^\s*\d+(?:\.\d{1,2})?\s*$")


class ValidationError(Exception):
    """请求参数非法（映射为 HTTP 400）。"""


def _parse_decimal(raw, field: str, upper: Decimal) -> Decimal:
    if isinstance(raw, bool):
        raise ValidationError(f"{field} 必须是数值")
    if isinstance(raw, (int, float)):
        text = format(raw, ".15f").rstrip("0").rstrip(".")
    elif isinstance(raw, str):
        text = raw
    else:
        raise ValidationError(f"{field} 必须是数值")
    if not _NUM_RE.match(text):
        raise ValidationError(
            f"{field} 必须是至多两位小数的正数（收到 {raw!r}）"
        )
    try:
        d = Decimal(text.strip())
    except InvalidOperation:
        raise ValidationError(f"{field} 必须是数值")
    if d <= 0:
        raise ValidationError(f"{field} 必须为正数")
    if d > upper:
        raise ValidationError(f"{field} 不得超过 {upper:g}")
    return d


def validate_payload(payload: dict) -> list[Group]:
    """校验并归一化整份录入，返回 Group 列表（按录入顺序）。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    name = payload.get("name", "")
    if name is not None and not isinstance(name, str):
        raise ValidationError("试算名称必须为字符串")

    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, list):
        raise ValidationError("groups 必须为数组")
    if not (MIN_GROUPS <= len(raw_groups) <= MAX_GROUPS):
        raise ValidationError(
            f"旁路组数量必须在 {MIN_GROUPS}～{MAX_GROUPS} 个之间"
        )

    groups: list[Group] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw_groups):
        if not isinstance(item, dict):
            raise ValidationError(f"第 {idx + 1} 个组格式错误")
        gid = item.get("id")
        if not isinstance(gid, str):
            raise ValidationError(f"第 {idx + 1} 个组的编号必须为字符串")
        gid = gid.strip()
        if not gid or len(gid) > MAX_ID_LEN:
            raise ValidationError(
                f"第 {idx + 1} 个组的编号必须为 1～{MAX_ID_LEN} 个非空字符"
            )
        if gid in seen:
            raise ValidationError(f"组编号 {gid!r} 重复，必须唯一")
        seen.add(gid)
        voc = float(_parse_decimal(item.get("voc"),
                                   f"组 {gid} 的开路电压 Voc", MAX_DECIMAL))
        isc = float(_parse_decimal(item.get("isc"),
                                   f"组 {gid} 的短路电流 Isc", MAX_CURRENT))
        groups.append(Group(gid, voc, isc))

    _validate_baseline(payload.get("baseline"), seen)
    return groups


def _validate_baseline(raw, ids: set[str]) -> None:
    """基线接线：1～3 个非空串，每组恰好出现一次。"""
    if not isinstance(raw, list):
        raise ValidationError("baseline 必须为数组")
    if not (1 <= len(raw) <= MAX_STRINGS):
        raise ValidationError(
            f"基线接线必须由 1～{MAX_STRINGS} 条串组成"
        )
    used: list[str] = []
    for si, string in enumerate(raw):
        if not isinstance(string, list) or not string:
            raise ValidationError(f"基线第 {si + 1} 串必须是非空数组")
        for gid in string:
            if not isinstance(gid, str) or gid not in ids:
                raise ValidationError(
                    f"基线第 {si + 1} 串中的组编号 {gid!r} 不存在"
                )
            if gid in used:
                raise ValidationError(
                    f"基线中组 {gid!r} 出现多次，每组必须恰出现一次"
                )
            used.append(gid)
    if set(used) != ids:
        missing = sorted(ids - set(used))
        raise ValidationError(
            "基线接线中每组必须恰出现一次，缺失：" + "、".join(missing)
        )
