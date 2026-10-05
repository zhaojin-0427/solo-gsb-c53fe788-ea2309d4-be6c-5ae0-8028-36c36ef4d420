"""现场电流偏差定位。

对一条**已保存的历史试算记录**，从其保存的组参数与基线接线重新构建
分段串曲线（绝不使用结果快照中那套稀疏采样做插值），在用户提交的实测
端电压测点上求理论电流，并比较两类假设：

1. 健康状态：全部组参数与记录一致；
2. 单组 Isc 退化：恰有一组短路电流降为某个**小于原 Isc 的正数、至多
   两位小数**的值（Voc 不变，接线不变）。

每个测点都满足 |实测电流 − 理论电流| ≤ 容差，该假设才算“可解释”。

拐点处由 ``_segment_at`` 精确选段（分段连续，不会因选段产生跳变）；
端电压达到某串 Voc 的支路电流为 0、对该串内各组的退化不敏感，
这些事实由 ``string_current`` / ``total_current`` 精确处理，枚举网格
无需在拐点上做任何插值。
"""

import math
from typing import List, Sequence

from .model import Group, MERGE_EPS, string_curve, total_current

# 理论电流与实测电流比较时的浮点容差（输入量化到 0.01，远小于物理量纲）
_EPS = 1e-9
# 单组 Isc 退化假设的量化步长：至多两位小数
GRID = 0.01


class DiagnoseError(ValueError):
    """诊断入参非法（调用方映射为 HTTP 400）。"""


def _finite_nonneg(raw, field: str, *, allow_zero: bool = True) -> float:
    """解析有限非负数。

    接受 int/float（拒绝 bool）或可被 float() 解析为有限数的字符串
    （允许 "1.5"、"1e3" 等）；NaN / Infinity / None / 非数值文本一律拒绝。
    """
    if isinstance(raw, bool):
        raise DiagnoseError(f"{field} 必须是有限数值")
    if isinstance(raw, int):
        value = float(raw)
    elif isinstance(raw, float):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            value = float(text)
        except ValueError:
            raise DiagnoseError(f"{field} 必须是有限数值（收到 {raw!r}）")
    else:
        raise DiagnoseError(f"{field} 必须是有限数值")
    if not math.isfinite(value):
        raise DiagnoseError(f"{field} 必须是有限数值")
    if value < 0.0 or (not allow_zero and value <= 0.0):
        raise DiagnoseError(f"{field} 必须为非负数")
    return value


def validate_diagnose_request(payload: dict, baseline_voc: float) -> List[tuple]:
    """校验诊断请求，返回 [(电压 V, 电流 I), ...]（电压两两互异）。

    规则：
    - points 至少 3 个；每个点含 v、i，均为有限非负数；
    - 电压可为零，但不得超过该记录基线开路电压 baseline_voc；
    - 电压必须两两互异；
    - tolerance 为有限非负数（可为零）。
    """
    if not isinstance(payload, dict):
        raise DiagnoseError("请求体必须是 JSON 对象")

    if "tolerance" not in payload:
        raise DiagnoseError("缺少电流容差 tolerance")
    tol = _finite_nonneg(payload.get("tolerance"), "电流容差")

    raw_points = payload.get("points")
    if not isinstance(raw_points, list):
        raise DiagnoseError("points 必须为数组")
    if len(raw_points) < 3:
        raise DiagnoseError("至少提交 3 个实测 (V,I) 测点")

    points: List[tuple] = []
    seen_v: set = set()
    for idx, p in enumerate(raw_points):
        if not isinstance(p, dict):
            raise DiagnoseError(f"第 {idx + 1} 个测点格式错误")
        v = _finite_nonneg(p.get("v"), f"第 {idx + 1} 个测点的电压")
        i = _finite_nonneg(p.get("i"), f"第 {idx + 1} 个测点的电流")
        if v > baseline_voc + 1e-7:
            raise DiagnoseError(
                f"第 {idx + 1} 个测点电压 {_num(v)} V 超过该记录基线开路电压 "
                f"{_num(baseline_voc)} V"
            )
        # 电压按原始数值去重（浮点输入，精确比较；字符串经 float 归一化）
        v_key = round(v, 12)
        if v_key in seen_v:
            raise DiagnoseError(f"第 {idx + 1} 个测点电压与前点重复，电压必须互异")
        seen_v.add(v_key)
        points.append((v, i))

    payload["tolerance"] = tol  # 回写归一化值，供主流程使用
    return points


def _num(x: float) -> str:
    """去掉无意义尾零的紧凑数值文本。"""
    return f"{x:.10f}".rstrip("0").rstrip(".")


def _grid_values(isc0: float) -> List[float]:
    """小于 isc0 的全部正两位小数候选（升序）：0.01, 0.02, ..., < isc0。"""
    n = int(math.floor(isc0 / GRID + 1e-9))
    if n * GRID >= isc0 - 1e-9:
        n -= 1  # 等档（原 Isc 本身）不属于退化假设
    return [k * GRID for k in range(1, n + 1)]


def _within(theory: float, measured: float, tol: float) -> bool:
    return abs(theory - measured) <= tol + _EPS


def diagnose(groups: Sequence[Group], baseline: Sequence[Sequence[str]],
             points: Sequence[tuple], tol: float) -> dict:
    """执行现场偏差定位。

    返回结构见模块开头说明：含健康假设逐点偏差、单组退化可行候选的
    Isc 网格区间，以及最终中文结论。
    """
    by_id = {g.id: g for g in groups}
    # 基线串曲线：组内顺序无关，按编号排序以与试算主流程保持一致
    base_strings = [
        [by_id[gid] for gid in sorted(s, key=lambda g: by_id[g].id)]
        for s in baseline
    ]
    base_curves = [string_curve(s) for s in base_strings]
    baseline_voc = max(c.voc for c in base_curves)

    # 组 -> 所在 (串索引, 串内位置)，退化时只重建这一条串
    string_of_group = {}
    for s_idx, string in enumerate(base_strings):
        for pos, g in enumerate(string):
            string_of_group[g.id] = (s_idx, pos)

    # ---- 健康假设：全部测点理论电流与逐点偏差 ----
    healthy_points = []
    healthy_ok = True
    for v, i_meas in points:
        i_th = total_current(base_curves, v)
        dev = i_meas - i_th
        fit = _within(i_th, i_meas, tol)
        healthy_ok = healthy_ok and fit
        healthy_points.append({
            "v": v, "i_measured": i_meas,
            "i_theory_healthy": _r(i_th), "deviation": _r(dev),
            "within_tolerance": fit,
        })

    # ---- 单组 Isc 退化假设：枚举每组 0.01 网格上的全部小于原值的正数 ----
    group_results = []
    for g in groups:
        s_idx, pos = string_of_group[g.id]
        others = [c for j, c in enumerate(base_curves) if j != s_idx]
        # 该组所在串“端电压严格小于其 Voc、仍贡献非零电流”的测点数量
        # （仅用于结果展示；V 恰等于 Voc 时该串恒为零，对组退化不敏感）。
        # 注意：不在此处跳过这些测点——开路支路对总电流贡献 0，
        # 该组退化不会改变总电流，若实测偏离健康值则候选自然全部落空，
        # 不能因“无约束”而真空判为可行。
        sensitive_points = sum(
            1 for v, _ in points if v < base_curves[s_idx].voc - MERGE_EPS)
        feasible = []
        for isc_new in _grid_values(g.isc):
            degraded = Group(g.id, g.voc, isc_new)
            members = [
                degraded if k == pos else mg
                for k, mg in enumerate(base_strings[s_idx])
            ]
            curve_new = string_curve(members)
            ok = True
            for v, i_meas in points:
                i_th = total_current(others + [curve_new], v)
                if not _within(i_th, i_meas, tol):
                    ok = False
                    break
            if ok:
                feasible.append(isc_new)
        entry = {
            "group": g.id,
            "isc_original": _r(g.isc),
            "sensitive_points": sensitive_points,
            "feasible": bool(feasible),
        }
        if feasible:
            entry["isc_min"] = _r2(feasible[0])
            entry["isc_max"] = _r2(feasible[-1])
            entry["isc_candidates"] = len(feasible)
        group_results.append(entry)

    explainable = [e for e in group_results if e["feasible"]]

    # ---- 结论判定 ----
    if healthy_ok:
        verdict = "unconfirmed"
        message = ("健康状态即可解释全部测点偏差，无法确认异常"
                   + ("；同时存在可解释的单组退化假设，但不足以判定为故障。"
                      if explainable else "。"))
    elif len(explainable) == 0:
        verdict = "out_of_model"
        message = ("全部健康与单组 Isc 退化假设均无法解释测点偏差："
                   "模型外偏差（可能存在多组退化、Voc 漂移或接线改变）。")
    elif len(explainable) == 1:
        e = explainable[0]
        verdict = "single_group"
        if e["isc_min"] == e["isc_max"]:
            rng = f"Isc = {e['isc_min']}"
        else:
            rng = f"{e['isc_min']} ≤ Isc ≤ {e['isc_max']}"
        message = (f"定位到单组退化：组 {e['group']} 的短路电流可解释全部"
                   f"测点偏差，可行 Isc 范围 {rng} A（共 "
                   f"{e['isc_candidates']} 个 0.01 网格取值，"
                   f"原 Isc {e['isc_original']} A）。")
    else:
        verdict = "ambiguous"
        names = "、".join(
            f"组 {e['group']}（{e['isc_min']}～{e['isc_max']} A）"
            if e["isc_min"] != e["isc_max"]
            else f"组 {e['group']}（{e['isc_min']} A）"
            for e in explainable
        )
        message = f"存在多个可解释的单组退化假设，组间歧义：{names}。"

    return {
        "baseline_voc": _r(baseline_voc),
        "tolerance": tol,
        "points": healthy_points,
        "healthy_explainable": healthy_ok,
        "verdict": verdict,
        "message": message,
        "groups": group_results,
        "explainable_groups": [e["group"] for e in explainable],
    }


def _r(x: float) -> float:
    """理论电流/偏差保留 6 位小数。"""
    v = round(float(x), 6)
    return 0.0 if v == 0 else v


def _r2(x: float) -> float:
    """退化 Isc 网格值保留 2 位小数。"""
    return round(float(x) + 0.0, 2)
