"""SQLite 存储：每条试算保存输入与结果 JSON 快照，可按 id 重开。"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "trials.db"


def _db_path() -> Path:
    directory = os.environ.get("PVBENCH_DB_DIR")
    if directory:
        return Path(directory) / "trials.db"
    return _DEFAULT_DB


DB_PATH = _db_path()

_CREATE = """
CREATE TABLE IF NOT EXISTS trials (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    result     TEXT    NOT NULL
)
"""

# 迁移标记（如结果快照格式版本），避免每次启动重复校正
_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE)
    conn.execute(_CREATE_META)
    return conn


def create_trial(name: str, payload: dict, result: dict,
                 db_path: Path | None = None) -> dict:
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO trials (name, created_at, payload, result) "
            "VALUES (?, ?, ?, ?)",
            (name or f"试算 {created}", created,
             json.dumps(payload, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False)),
        )
        trial_id = cur.lastrowid
    return {"id": trial_id, "name": name or f"试算 {created}",
            "created_at": created}


def list_trials(db_path: Path | None = None) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, payload FROM trials "
            "ORDER BY id DESC"
        ).fetchall()
    items = []
    for r in rows:
        payload = json.loads(r["payload"])
        items.append({
            "id": r["id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "num_groups": len(payload.get("groups", [])),
            "num_baseline_strings": len(payload.get("baseline", [])),
        })
    return items


def get_trial(trial_id: int, db_path: Path | None = None) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM trials WHERE id = ?", (trial_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "payload": json.loads(row["payload"]),
        "result": json.loads(row["result"]),
    }


def delete_trial(trial_id: int, db_path: Path | None = None) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM trials WHERE id = ?", (trial_id,))
        return cur.rowcount > 0


def get_meta(key: str, db_path: Path | None = None) -> str | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row is not None else None


def set_meta(key: str, value: str, db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def all_snapshots(db_path: Path | None = None) -> list[dict]:
    """全部记录的 (id, 输入, 结果快照)，供启动迁移按输入校正结果。"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, payload, result FROM trials ORDER BY id"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "payload": json.loads(r["payload"]),
            "result": json.loads(r["result"]),
        }
        for r in rows
    ]


def update_trial_result(trial_id: int, result: dict,
                        db_path: Path | None = None) -> None:
    """就地更新已有记录的结果快照（编号、创建时间、输入均不变）。"""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE trials SET result = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), trial_id),
        )
