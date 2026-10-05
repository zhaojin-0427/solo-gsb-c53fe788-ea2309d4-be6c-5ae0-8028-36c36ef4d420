"""光伏旁路组重接线试算台 —— FastAPI 入口。"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .diagnose import DiagnoseError, diagnose, validate_diagnose_request
from .model import Group, evaluate_wiring, find_optimal, string_curve
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


@app.exception_handler(DiagnoseError)
async def _diagnose_handler(_: Request, exc: DiagnoseError):
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
    """现场电流偏差定位：只读历史记录，按保存的组参数与基线接线重算。

    不改动原试算结果与重开数据（不写库）；记录不存在返回 404，
    测点非法返回 400。
    """
    trial = db.get_trial(trial_id)
    if trial is None:
        return JSONResponse(status_code=404,
                            content={"detail": "试算记录不存在"})
    payload = trial["payload"]
    try:
        groups = validate_payload(payload)
        by_id = {g.id: g for g in groups}
        baseline_curves = [
            string_curve([by_id[gid]
                          for gid in sorted(s, key=lambda g: by_id[g].id)])
            for s in payload["baseline"]
        ]
        baseline_voc = max(c.voc for c in baseline_curves)

        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DiagnoseError("请求体不是合法的 JSON")
        points = validate_diagnose_request(body, baseline_voc)
        tol = float(body["tolerance"])
        result = await run_in_threadpool(
            diagnose, groups, payload["baseline"], points, tol)
    except DiagnoseError:
        raise
    return {"id": trial["id"], "name": trial["name"],
            "diagnosis": result}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
