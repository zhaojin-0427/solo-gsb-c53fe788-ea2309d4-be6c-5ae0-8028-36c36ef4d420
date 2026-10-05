"""光伏旁路组重接线试算台 —— FastAPI 入口。"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .diagnosis import diagnose, validate_diagnosis_payload
from .model import Group, evaluate_wiring, find_optimal
from .validation import ValidationError, validate_payload

# 结果快照版本：2 起零电压电流修正为“每串 max(Isc)、并联按串求和”
RESULT_VERSION = "2"


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # 启动时按保存的输入校正旧结果快照；版本一致（如容器重启）即为空操作
    await run_in_threadpool(_migrate_results)
    yield


app = FastAPI(title="光伏旁路组重接线试算台", version="1.0.1",
              lifespan=_lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.exception_handler(ValidationError)
async def _validation_handler(_: Request, exc: ValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def _r6(x: float) -> float:
    """结果统一保留 6 位小数（输入仅 2 位，足够消除中间误差）。"""
    v = round(float(x), 6)
    return 0.0 if v == 0 else v


def _serialize_wiring(res) -> dict:
    m = res.mpp
    curve = res.curve
    return {
        "strings": [list(s) for s in res.strings],
        "num_strings": res.num_strings,
        "string_voc": [_r6(v) for v in res.string_voc],
        "voc": _r6(res.voc),
        "i_at_zero": _r6(res.i_at_zero),
        "peak": {"v": _r6(m.v), "i": _r6(m.i), "p": _r6(m.p)},
        "curve": {
            "v": [_r6(v) for v in curve["v"]],
            "i": [_r6(i) for i in curve["i"]],
            "p": [_r6(p) for p in curve["p"]],
            "knees": [_r6(k) for k in curve["knees"]],
        },
    }


def _canonical_strings(strings) -> tuple:
    """把接线规范化为“组内排序 + 串按字典序”的元组（与串的录入顺序无关）。"""
    return tuple(sorted(tuple(sorted(s)) for s in strings))


def _solve(groups: list[Group], baseline: list[list[str]]) -> dict:
    by_id = {g.id: g for g in groups}

    # 基线：保留串的录入顺序，组内编号排序（串联与组序无关）
    base_wiring = [
        [by_id[gid] for gid in sorted(string, key=lambda g: by_id[g].id)]
        for string in baseline
    ]
    baseline_res = evaluate_wiring(base_wiring)

    optimal_res, candidates = find_optimal(groups)

    bp, op = baseline_res.mpp.p, optimal_res.mpp.p
    gain = op - bp
    gain_pct = (gain / bp * 100.0) if bp > 0 else 0.0
    return {
        "baseline": _serialize_wiring(baseline_res),
        "optimal": _serialize_wiring(optimal_res),
        "comparison": {
            "gain_power": _r6(gain),
            "gain_power_pct": _r6(gain_pct),
            "gain_voltage": _r6(optimal_res.mpp.v - baseline_res.mpp.v),
            "same_wiring": _canonical_strings(baseline_res.strings)
                           == _canonical_strings(optimal_res.strings),
        },
        "candidates": [
            {
                "strings": c["strings"],
                "num_strings": c["num_strings"],
                "peak_voltage": _r6(c["peak_voltage"]),
                "peak_power": _r6(c["peak_power"]),
            }
            for c in candidates
        ],
    }


def _migrate_results() -> None:
    """把旧版结果快照按保存的输入重算并就地更新。

    只 UPDATE 已有行的 result：编号、创建时间、输入保持不变，不新增记录；
    模型修正不影响峰值与候选排序，仅零电压电流与 I-V 首点被校正。
    """
    if db.get_meta("result_version") == RESULT_VERSION:
        return
    for row in db.all_snapshots():
        try:
            groups = validate_payload(row["payload"])
            fresh = _solve(groups, row["payload"]["baseline"])
        except Exception:
            continue  # 无法复算的记录保留原快照
        if fresh != row["result"]:
            db.update_trial_result(row["id"], fresh)
    db.set_meta("result_version", RESULT_VERSION)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/trials")
async def create_trial(request: Request):
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400,
                            content={"detail": "请求体不是合法的 JSON"})

    groups = validate_payload(payload)
    result = await run_in_threadpool(_solve, groups, payload["baseline"])
    saved = await run_in_threadpool(
        db.create_trial, payload.get("name", ""), payload, result)
    return {"id": saved["id"], "name": saved["name"],
            "created_at": saved["created_at"], "result": result}


@app.get("/api/trials")
async def list_trials():
    return {"trials": db.list_trials()}


@app.get("/api/trials/{trial_id}")
async def get_trial(trial_id: int):
    trial = db.get_trial(trial_id)
    if trial is None:
        return JSONResponse(status_code=404,
                            content={"detail": "试算记录不存在"})
    return trial


@app.delete("/api/trials/{trial_id}")
async def delete_trial(trial_id: int):
    if not db.delete_trial(trial_id):
        return JSONResponse(status_code=404,
                            content={"detail": "试算记录不存在"})
    return {"deleted": trial_id}


@app.post("/api/trials/{trial_id}/diagnose")
async def diagnose_trial(trial_id: int, request: Request):
    """现场电流偏差定位：按记录保存的组参数与基线接线重建曲线，
    比较健康与单组 Isc 下降两类假设。只读，不改动原试算与重开结果。"""
    trial = await run_in_threadpool(db.get_trial, trial_id)
    if trial is None:
        return JSONResponse(status_code=404,
                            content={"detail": "试算记录不存在"})

    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(status_code=400,
                            content={"detail": "请求体不是合法的 JSON"})

    # 用保存的输入重新校验/重建（不信任也不使用稀疏结果快照做插值）
    groups = validate_payload(trial["payload"])
    by_id = {g.id: g for g in groups}
    baseline_voc = max(
        sum(by_id[gid].voc for gid in string_ids)
        for string_ids in trial["payload"]["baseline"]
    )
    points, tol = validate_diagnosis_payload(payload, baseline_voc)
    result = await run_in_threadpool(
        diagnose, groups, trial["payload"]["baseline"], points, tol)
    result = _serialize_diagnosis(result)
    result["id"] = trial_id
    result["name"] = trial["name"]
    return result


def _serialize_diagnosis(result: dict) -> dict:
    def pt(p: dict, keys: tuple) -> dict:
        return {**{k: p[k] for k in keys},
                "within_tolerance": p["within_tolerance"]}

    out = {
        "verdict": result["verdict"],
        "message": result["message"],
        "baseline_voc": _r6(result["baseline_voc"]),
        "tolerance": _r6(result["tolerance"]),
        "healthy_explainable": result["healthy_explainable"],
        "points": [
            pt(p, ("v", "i", "theoretical_i", "deviation"))
            for p in result["points"]
        ],
        "groups": [],
    }
    for g in result["groups"]:
        item = {
            "group_id": g["group_id"],
            "isc_original": _r6(g["isc_original"]),
            "explainable": g["explainable"],
        }
        if g["explainable"]:
            item["isc_min"] = _r6(g["isc_min"])
            item["isc_max"] = _r6(g["isc_max"])
            item["points"] = [
                pt(p, ("v", "i", "theoretical_i_min", "theoretical_i_max"))
                for p in g["points"]
            ]
        out["groups"].append(item)
    return out


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
