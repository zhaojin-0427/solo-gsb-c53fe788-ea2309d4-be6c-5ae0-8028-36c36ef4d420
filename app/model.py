"""光伏旁路组接线电气模型与全接线搜索。

单组模型（线性旁路，电压非负截断）::

    V_g(I) = max(Voc_g * (1 - I / Isc_g), 0)

一条串由若干组串联，串电压为各组电压之和::

    V_S(I) = Σ_g V_g(I)

V(I) 连续、分段线性、不增：I=0 时取串开路电压 Voc_S = ΣVoc；
随 I 增大，Isc 较小的组依次在各自 Isc 处旁路（“旁路拐点”），
I ≥ max Isc 后串电压为 0。因此反函数 I_S(V) 在 [0, Voc_S] 上良定义。

若干条串并联共用端电压 V::

    I(V) = Σ_S I_S(V)          （V ≥ Voc_S 的串贡献为 0）
    P(V) = V · I(V)

把各串全部旁路拐点（含 0 与各串 Voc）取并集排序后，每个电压区间内
I(V) 为线性、P(V) 为开口向下的二次函数，故逐段只需比较：
各拐点（端点）与二次段内部极值。零电压处取最小可行电流 I(0)。
"""

from collections import OrderedDict
from dataclasses import dataclass
from itertools import product
from typing import List, Sequence, Tuple

# 电压断点合并容差（输入量化到 0.01，正常拐点间距远大于此）
MERGE_EPS = 1e-10
# 峰值功率判同的相对/绝对容差（用于同功率并列排序）
POWER_REL_TOL = 1e-9
POWER_ABS_TOL = 1e-12

MAX_STRINGS = 3


@dataclass(frozen=True)
class Group:
    """一个旁路组：唯一编号、开路电压 Voc(V)、短路电流 Isc(A)。"""

    id: str
    voc: float
    isc: float


@dataclass(frozen=True)
class LinearSeg:
    """电压区间 [v_lo, v_hi] 上 I(V) = a - b·V（串的一段 V-I 关系）。"""

    v_lo: float
    v_hi: float
    a: float
    b: float


@dataclass(frozen=True)
class StringCurve:
    """一条串的分段 V-I 反函数。knees 为升序拐点（首 0、末 Voc）。"""

    voc: float
    i_at_zero: float
    knees: Tuple[float, ...]
    segments: Tuple[LinearSeg, ...]


def string_curve(groups: Sequence[Group]) -> StringCurve:
    """求一条串的 I(V) 分段线性关系。

    按 Isc 降序设去重阈值 s_1 > s_2 > ... > s_m（s_{m+1}=0）。
    电流区间 [s_{k+1}, s_k] 内活动组为 Isc ≥ s_k 的组::

        V(I) = A_k - B_k I,  A_k = Σ Voc, B_k = Σ Voc/Isc
    对应电压区间 [w_{k+1}, w_k]，w_k = V(s_k)，反函数为线性。
    零电压最小可行电流为最高档 s_1 = max Isc：I ≥ s_1 时全部组旁路、
    串电压归零，故 I(0) = A_1 / B_1 = s_1（即首段系数）。
    """
    if not groups:
        raise ValueError("string must contain at least one group")

    buckets = OrderedDict()
    for g in sorted(groups, key=lambda g: (-g.isc, g.id)):
        buckets.setdefault(g.isc, []).append(g)

    voc_total = sum(g.voc for g in groups)
    cum = []          # cum[k-1] = (A_k, B_k)
    w = []            # w[k-1] = w_k = V(s_k)，k=1..m
    a_sum = b_sum = 0.0
    for level, members in buckets.items():
        for g in members:
            a_sum += g.voc
            b_sum += g.voc / g.isc
        cum.append((a_sum, b_sum))
        w.append(a_sum - b_sum * level)
    w.append(voc_total)  # w_{m+1} = V(0) = ΣVoc
    # 浮点钳制：w_1 应为 0，w_{m+1} 应为 Voc 总和
    w[0] = 0.0
    w[-1] = max(0.0, w[-1])

    segments: List[LinearSeg] = []
    m = len(cum)
    # 电流 I 从 s_k 降到 s_{k+1} 时电压从 w_k 升到 w_{k+1}，
    # 故电压升序区间为 [w_{k-1}(=w_k), w[k](=w_{k+1})]
    for k in range(1, m + 1):
        a_k, b_k = cum[k - 1]
        v_lo, v_hi = w[k - 1], w[k]
        if v_hi - v_lo > MERGE_EPS:
            segments.append(LinearSeg(v_lo, v_hi, a_k / b_k, 1.0 / b_k))

    # V=0 的最小可行电流：I 升至最高 Isc 档 s_1 时全部组旁路、串电压归零，
    # 故 I(0) = s_1 = max Isc = A_1 / B_1（首段系数，与正电压首段自然衔接）
    i_at_zero = cum[0][0] / cum[0][1]
    return StringCurve(
        voc=w[-1],
        i_at_zero=i_at_zero,
        knees=tuple(w),
        segments=tuple(segments),
    )


def _segment_at(curve: StringCurve, v: float) -> LinearSeg | None:
    """定位串在电压 v 处所在的线性段；v 超过串 Voc 返回 None（该串零电流）。"""
    if v > curve.voc + MERGE_EPS:
        return None
    for seg in curve.segments:
        if seg.v_lo - MERGE_EPS <= v <= seg.v_hi + MERGE_EPS:
            return seg
    return curve.segments[0]  # 数值边界兜底（最低段左端即 0）


def string_current(curve: StringCurve, v: float) -> float:
    """单条串在端电压 v 处的理论电流 I_S(v)。

    v 达到（或超过）该串开路电压 Voc 时串电流为 0（支路已开路）；
    落在拐点上时，分段连续故取相邻段结果一致，统一按 _segment_at 选段。
    """
    seg = _segment_at(curve, v)
    if seg is None:
        return 0.0
    return max(0.0, seg.a - seg.b * v)


def total_current(curves: Sequence[StringCurve], v: float) -> float:
    """并联总理论电流：各串在同一端电压 v 下反解后求和（达 Voc 的串贡献 0）。"""
    return sum(string_current(c, v) for c in curves)


def _union_knees(curves: Sequence[StringCurve]) -> List[float]:
    """所有串拐点（含 0、各自 Voc 与最大 Voc）的升序去并集。"""
    vals = [0.0]
    v_max = 0.0
    for c in curves:
        vals.extend(c.knees)
        v_max = max(v_max, c.voc)
    vals.append(v_max)
    vals.sort()
    merged: List[float] = []
    for v in vals:
        if not merged or v - merged[-1] > MERGE_EPS:
            merged.append(v)
        else:
            merged[-1] = min(merged[-1], v)
    if merged:
        merged[0] = 0.0
        merged[-1] = v_max
    return merged


@dataclass(frozen=True)
class ParSeg:
    """并联总电流的一段：v∈[lo,hi] 上 I(V)=a-bV，P=aV-bV²。"""

    v_lo: float
    v_hi: float
    a: float
    b: float


def parallel_segments(curves: Sequence[StringCurve]) -> List[ParSeg]:
    """求并联组合在各电压段的总电流线性系数。"""
    breaks = _union_knees(curves)
    segs: List[ParSeg] = []
    for lo, hi in zip(breaks, breaks[1:]):
        if hi - lo <= MERGE_EPS:
            continue
        mid = (lo + hi) / 2.0
        a_tot = b_tot = 0.0
        active = False
        for c in curves:
            seg = _segment_at(c, mid)
            if seg is not None:
                active = True
                a_tot += seg.a
                b_tot += seg.b
        if active:
            segs.append(ParSeg(lo, hi, a_tot, b_tot))
    return segs


@dataclass(frozen=True)
class MppPoint:
    v: float
    i: float
    p: float


def _current(seg: ParSeg, v: float) -> float:
    return max(0.0, seg.a - seg.b * v)


def find_mpp(segs: Sequence[ParSeg]) -> MppPoint:
    """逐段扫描旁路拐点与功率二次段内部极值，取全局最大功率点。

    同一段/同一接线出现等高峰值时取较小电压。
    """
    best_v, best_i, best_p = 0.0, 0.0, -1.0

    def consider(v: float, i: float, p: float) -> None:
        nonlocal best_v, best_i, best_p
        if p > best_p + POWER_ABS_TOL:
            best_v, best_i, best_p = v, i, p
        elif abs(p - best_p) <= max(
            POWER_ABS_TOL, POWER_REL_TOL * max(abs(p), abs(best_p))
        ) and v < best_v:
            best_v, best_i, best_p = v, i, p

    # 各段端点（首段 v_lo=0 即零电压最小可行电流点，功率 0）与二次顶点
    for seg in segs:
        lo, hi = seg.v_lo, seg.v_hi
        i_lo, i_hi = _current(seg, lo), _current(seg, hi)
        consider(lo, i_lo, lo * i_lo)
        consider(hi, i_hi, hi * i_hi)
        # P(V) = aV - bV²，凹二次函数；内部顶点 V* = a/(2b)
        if seg.b > 0.0:
            v_star = seg.a / (2.0 * seg.b)
            if lo + MERGE_EPS < v_star < hi - MERGE_EPS:
                i_star = _current(seg, v_star)
                consider(v_star, i_star, v_star * i_star)

    if best_p < 0.0:
        best_v = best_i = best_p = 0.0
    return MppPoint(best_v, best_i, max(0.0, best_p))


def sample_curve(segs: Sequence[ParSeg], i_at_zero: float,
                 points_per_seg: int = 24) -> dict:
    """对 P-V / I-V 曲线逐段均匀采样（拐点天然重合，自动去重）。"""
    vs: List[float] = [0.0]
    ii: List[float] = [max(0.0, i_at_zero)]
    pp: List[float] = [0.0]
    knees: List[float] = []

    def push(v: float, i: float) -> None:
        if abs(v - vs[-1]) <= MERGE_EPS:
            return
        vs.append(v)
        ii.append(max(0.0, i))
        pp.append(v * max(0.0, i))

    for seg in segs:
        knees.append(seg.v_lo)
        for k in range(1, points_per_seg + 1):
            v = seg.v_lo + (seg.v_hi - seg.v_lo) * k / points_per_seg
            push(v, _current(seg, v))
    if segs:
        knees.append(segs[-1].v_hi)
    return {"v": vs, "i": ii, "p": pp, "knees": knees}


# ---------------------------------------------------------------- 接线枚举

def canonical_partitions(n: int, max_blocks: int = MAX_STRINGS):
    """枚举 n 个带标号元素分成 1..max_blocks 个非空块的全部划分。

    以“各块按最小元素升序（= 首次出现序）”为唯一规范表示，
    该表示同时是块排列中的字典序最小者，正好对应题目要求的
    “组内及串间编号字典序”比较基准。
    """
    cache_key = (n, max_blocks)
    cached = _PARTITION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    seen = set()
    out = []
    for labels in product(range(max_blocks), repeat=n):
        order: List[int] = []
        for lab in labels:
            if lab not in order:
                order.append(lab)
        norm = tuple(order.index(lab) for lab in labels)
        if norm in seen:
            continue
        seen.add(norm)
        blocks: dict = {}
        for idx, lab in enumerate(norm):
            blocks.setdefault(lab, []).append(idx)
        out.append(tuple(tuple(b) for b in blocks.values()))
    _PARTITION_CACHE[cache_key] = out
    return out


_PARTITION_CACHE: dict = {}


@dataclass(frozen=True)
class WiringResult:
    strings: Tuple[Tuple[str, ...], ...]
    num_strings: int
    string_voc: Tuple[float, ...]
    voc: float
    i_at_zero: float
    mpp: MppPoint
    curve: dict | None


def evaluate_wiring(strings: Sequence[Sequence[Group]],
                    precomputed: Sequence[StringCurve] | None = None,
                    with_curve: bool = True) -> WiringResult:
    """计算一份具体接线（组的序列的序列）的完整电气结果。

    with_curve=False 时跳过曲线采样（全量搜索阶段只需峰值）。
    """
    curves = list(precomputed) if precomputed is not None \
        else [string_curve(s) for s in strings]
    segs = parallel_segments(curves)
    mpp = find_mpp(segs)
    i_zero = sum(c.i_at_zero for c in curves)
    ids = tuple(tuple(g.id for g in s) for s in strings)
    return WiringResult(
        strings=ids,
        num_strings=len(strings),
        string_voc=tuple(c.voc for c in curves),
        voc=max(c.voc for c in curves),
        i_at_zero=i_zero,
        mpp=mpp,
        curve=sample_curve(segs, i_zero) if with_curve else None,
    )


def _same_power(p1: float, p2: float) -> bool:
    return abs(p1 - p2) <= max(
        POWER_ABS_TOL, POWER_REL_TOL * max(abs(p1), abs(p2), 1.0)
    )


def find_optimal(groups: Sequence[Group]) -> Tuple[WiringResult, list]:
    """搜索全部 1～3 串接线，返回 (最优结果, 全部候选摘要表)。

    排序键：全局最大功率为主；同功率先取串数少；再按组内及串间编号
    字典序；仍并列时峰值取较小电压。
    """
    ordered = sorted(groups, key=lambda g: g.id)
    best: WiringResult | None = None
    best_part = None
    candidates = []

    # 串曲线只依赖“组集合”（串联与组内次序无关），按 frozenset 缓存
    curve_cache: dict = {}

    for part in canonical_partitions(len(ordered)):
        wiring, curves = [], []
        for block in part:
            key = frozenset(block)
            curve = curve_cache.get(key)
            if curve is None:
                sg = [ordered[i] for i in block]
                curve = string_curve(sg)
                curve_cache[key] = curve
            wiring.append([ordered[i] for i in block])
            curves.append(curve)
        res = evaluate_wiring(wiring, curves, with_curve=False)
        candidates.append({
            "strings": [list(s) for s in res.strings],
            "num_strings": res.num_strings,
            "peak_voltage": res.mpp.v,
            "peak_power": res.mpp.p,
        })
        if best is None:
            best, best_part = res, part
            continue
        bp = res.mpp.p
        if _same_power(bp, best.mpp.p):
            if (len(part), part, res.mpp.v) < (
                best.num_strings, best_part, best.mpp.v
            ):
                best, best_part = res, part
        elif bp > best.mpp.p:
            best, best_part = res, part

    assert best is not None
    candidates.sort(key=lambda c: (-c["peak_power"], c["num_strings"],
                                   c["strings"], c["peak_voltage"]))
    # 仅对最终胜出的接线做完整曲线采样
    best_wiring = [[ordered[i] for i in block] for block in best_part]
    best = evaluate_wiring(best_wiring, [curve_cache[frozenset(b)]
                                         for b in best_part], with_curve=True)
    return best, candidates
