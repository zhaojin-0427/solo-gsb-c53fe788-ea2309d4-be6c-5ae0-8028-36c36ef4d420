"""现场电流偏差定位。

对一条历史试算记录，用其**保存的组参数与基线接线**重新构建分段串曲线
（绝不使用结果快照里的稀疏采样做插值），在每个实测电压点求理论电流，
比较两类假设：

1. 健康：所有组参数与记录一致；
2. 恰有一组退化：该组 Isc 降为小于原值的正数（按 0.01 A 量化，至多
   两位小数），其余组与接线不变。

拐点选段规则与试算主流程完全一致（``model._segment_at``）：测点落在
旁路拐点上时取所在段；端电压达到某串开路电压（或更高）时该串电流为
零，不参与该点的电流贡献，也不提供关于该串内组的信息。

单调性：固定电压 V，串内某组 Isc 增大只会抬高（或不改变）该串电流，
故并联总电流对该组 Isc 单调不减。单个测点给出的可行 Isc 是一个区间，
全部测点求交后仍是区间，可用二分在 0.01 网格上定位可行段端点。
"""

import math
from typing import Dict, List, Sequence, Tuple

from .model import Group, StringCurve, string_curve, _segment_at
from .validation import ValidationError

MIN_POINTS = 3
MAX_POINTS = 1000
# 退化 Isc 的量化步长（至多两位小数）
ISC_GRID = 0.01
# 数值比较的浮点容差（仅吸收舍入误差，不放宽物理容差）
COMP_EPS = 1e-9


# ---------------------------------------------------------------- 入参校验

def _finite_number(raw, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValidationError(f"{field} 必须是有限数值（收到 {raw!r}）")
    value = float(raw)
    if not math.isfinite(value):
        raise ValidationError(f"{field} 必须是有限数值（收到 {raw!r}）")
    return value


def validate_diagnosis_payload(
    payload: dict, baseline_voc: float
) -> Tuple[List[Tuple[float, float]], float]:
    """校验诊断请求：≥3 个电压互异测点，V∈[0, 基线开路电压]，I≥0，容差≥0。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    tol = _finite_number(payload.get("tolerance"), "电流容差")
    if tol < 0:
        raise ValidationError("电流容差必须是非负数")

    raw_points = payload.get("points")
    if not isinstance(raw_points, list):
        raise ValidationError("points 必须是数组")
    if len(raw_points) < MIN_POINTS:
        raise ValidationError(f"至少提交 {MIN_POINTS} 个电压互异的实测点")
    if len(raw_points) > MAX_POINTS:
        raise ValidationError(f"实测点数量不得超过 {MAX_POINTS}")

    cap = baseline_voc * (1.0 + COMP_EPS) + COMP_EPS
    points: List[Tuple[float, float]] = []
    seen_v: set = set()
    for idx, item in enumerate(raw_points):
        where = f"第 {idx + 1} 个实测点"
        if not isinstance(item, dict):
            raise ValidationError(f"{where} 格式错误，应为含 v、i 的对象")
        v = _finite_number(item.get("v"), f"{where} 的电压 V")
        current = _finite_number(item.get("i"), f"{where} 的电流 I")
        if v < 0:
            raise ValidationError(f"{where} 的电压必须是非负数")
        if v > cap:
            raise ValidationError(
                f"{where} 的电压 {v:g} 超过该记录基线开路电压 "
                f"{baseline_voc:g} V"
            )
        if current < 0:
            raise ValidationError(f"{where} 的电流必须是非负数")
        if v in seen_v:
            raise ValidationError("各实测点的电压必须互异（不允许重复电压）")
        seen_v.add(v)
        points.append((v, current))
    return points, tol


# ---------------------------------------------------------------- 曲线求值

def string_current_at(curve: StringCurve, v: float) -> float:
    """串在端电压 v 处的电流；v 达到/超过串开路电压时该串电流为零。"""
    seg = _segment_at(curve, v)
    if seg is None:
        return 0.0
    return max(0.0, seg.a - seg.b * v)


def _close_enough(theory: float, measured: float, tol: float) -> bool:
    return abs(theory - measured) <= tol + COMP_EPS * max(
        1.0, abs(theory), abs(measured)
    )


# ---------------------------------------------------------------- 诊断主流程

def diagnose(
    groups: Sequence[Group],
    baseline: Sequence[Sequence[str]],
    points: Sequence[Tuple[float, float]],
    tol: float,
) -> dict:
    """比较健康与单组 Isc 下降两类假设，返回逐点/逐组的可解释性结果。"""
    by_id: Dict[str, Group] = {g.id: g for g in groups}
    # 与试算主流程一致：组内按编号排序（串联与组序无关），保留串序
    base_strings = [
        [by_id[gid] for gid in sorted(s, key=lambda x: by_id[x].id)]
        for s in baseline
    ]
    curves = [string_curve(s) for s in base_strings]
    baseline_voc = max(c.voc for c in curves)

    group_string_idx: Dict[str, int] = {}
    for si, members in enumerate(base_strings):
        for g in members:
            group_string_idx[g.id] = si

    def total_current(curves_override: Dict[int, StringCurve], v: float) -> float:
        total = 0.0
        for si, c in enumerate(curves):
            total += string_current_at(curves_override.get(si, c), v)
        return total

    # ---- 健康假设：逐点理论电流 ----
    healthy_currents = [total_current({}, v) for v, _ in points]
    healthy_within = [
        _close_enough(t, i, tol) for t, (_, i) in zip(healthy_currents, points)
    ]
    healthy_explainable = all(healthy_within)

    group_results: List[dict] = []
    for g in groups:
        si = group_string_idx[g.id]
        orig_isc = g.isc
        k_max = int(round(orig_isc / ISC_GRID))  # 原 Isc 对应的网格序号（排除）
        members = base_strings[si]

        # 重建该组所在串（其余串曲线与该组 Isc 无关，直接复用健康曲线）
        def degraded_currents(k: int) -> List[float]:
            degraded = Group(g.id, g.voc, k * ISC_GRID)
            rebuilt = [degraded if m.id == g.id else m for m in members]
            alt_curve = string_curve(rebuilt)
            return [
                total_current({si: alt_curve}, v) for v, _ in points
            ]

        lo_k = hi_k = None
        if k_max >= 2:  # 至少存在一个更小的正网格值 0.01
            # 欠流违例（理论 < 实测-容差）随 k 增大单调消失
            def low_violation(k: int) -> bool:
                return any(
                    t < i - tol - COMP_EPS * max(1.0, abs(t), abs(i))
                    for t, (_, i) in zip(degraded_currents(k), points)
                )

            # 过流违例（理论 > 实测+容差）随 k 增大单调出现
            def high_violation(k: int) -> bool:
                return any(
                    t > i + tol + COMP_EPS * max(1.0, abs(t), abs(i))
                    for t, (_, i) in zip(degraded_currents(k), points)
                )

            # 注意 k_max 用健康曲线参与边界定位，但不会成为可行候选
            if not low_violation(1):
                lo_k = 1
            elif not low_violation(k_max):
                left, right = 1, k_max
                while left < right:
                    mid = (left + right) // 2
                    if low_violation(mid):
                        left = mid + 1
                    else:
                        right = mid
                lo_k = left if left < k_max else None
            # else: 即使原 Isc 仍欠流，更小的 Isc 都不可行

            if lo_k is not None and not high_violation(1):
                left, right = 1, k_max
                while left < right:
                    mid = (left + right) // 2
                    if high_violation(mid):
                        right = mid
                    else:
                        left = mid + 1
                first_high = left if high_violation(left) else k_max + 1
                hi_k = min(first_high - 1, k_max - 1)
                if hi_k < lo_k:
                    lo_k = hi_k = None
            elif lo_k is not None:
                # 最小候选已过流，任何 k 都不满足
                lo_k = hi_k = None

        explainable = lo_k is not None
        entry = {
            "group_id": g.id,
            "isc_original": round(orig_isc, 2),
            "explainable": explainable,
        }
        if explainable:
            cur_lo = degraded_currents(lo_k)
            cur_hi = degraded_currents(hi_k)
            entry["isc_min"] = round(lo_k * ISC_GRID, 2)
            entry["isc_max"] = round(hi_k * ISC_GRID, 2)
            entry["points"] = [
                {
                    "v": v,
                    "i": i,
                    "theoretical_i_min": t_lo,
                    "theoretical_i_max": t_hi,
                    "within_tolerance": _close_enough(t_lo, i, tol)
                    and _close_enough(t_hi, i, tol),
                }
                for (v, i), t_lo, t_hi in zip(points, cur_lo, cur_hi)
            ]
        group_results.append(entry)

    points_detail = [
        {
            "v": v,
            "i": i,
            "theoretical_i": t,
            "deviation": t - i,
            "within_tolerance": ok,
        }
        for (v, i), t, ok in zip(points, healthy_currents, healthy_within)
    ]

    fitting = [r for r in group_results if r["explainable"]]
    if healthy_explainable:
        verdict, message = "healthy", (
            "健康状态即可解释全部测点（每个测点的偏差都在容差内），"
            "无法确认异常。"
        )
    elif len(fitting) == 1:
        r = fitting[0]
        verdict = "single"
        message = (
            f"仅同一组可解释：组 {r['group_id']} 的 Isc 需由 "
            f"{r['isc_original']:.2f} A 降至 "
            f"{r['isc_min']:.2f}～{r['isc_max']:.2f} A"
            "（小于原值的正数，至多两位小数），方可让全部测点偏差落在容差内。"
        )
    elif len(fitting) > 1:
        verdict = "ambiguous"
        detail = "、".join(
            f"组 {r['group_id']}（可行 Isc "
            f"{r['isc_min']:.2f}～{r['isc_max']:.2f} A）"
            for r in fitting
        )
        message = f"存在多组可单独解释测点偏差，无法唯一定位：{detail}。"
    else:
        verdict, message = "outside_model", (
            "健康状态与任一组 Isc 下降假设均无法解释全部测点，"
            "属于模型外偏差。"
        )

    return {
        "verdict": verdict,
        "message": message,
        "baseline_voc": baseline_voc,
        "tolerance": tol,
        "healthy_explainable": healthy_explainable,
        "points": points_detail,
        "groups": group_results,
    }
