from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with open_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY,
              type TEXT NOT NULL,
              status TEXT NOT NULL,
              stage TEXT NOT NULL,
              progress REAL NOT NULL,
              error TEXT,
              request_json TEXT NOT NULL,
              result_asset_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assets (
              asset_id TEXT PRIMARY KEY,
              type TEXT NOT NULL,
              path TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS presets (
              preset_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              request_json TEXT NOT NULL,
              thumbnail_asset_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def create_task(
    db_path: Path,
    *,
    task_id: str,
    task_type: str,
    request: dict[str, Any],
) -> None:
    now = utc_now_iso()
    with open_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks (
              task_id, type, status, stage, progress, error, request_json,
              result_asset_id, created_at, updated_at, started_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                task_type,
                "queued",
                "queued",
                0.0,
                None,
                json.dumps(request),
                None,
                now,
                now,
                None,
                None,
            ),
        )
        conn.commit()


def list_task_ids_by_status(db_path: Path, statuses: Iterable[str]) -> list[str]:
    statuses_list = list(statuses)
    if not statuses_list:
        return []

    placeholders = ",".join(["?"] * len(statuses_list))

    with open_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT task_id FROM tasks WHERE status IN ({placeholders}) ORDER BY created_at ASC",
            tuple(statuses_list),
        ).fetchall()
        return [row["task_id"] for row in rows]


def get_task(db_path: Path, task_id: str) -> Optional[dict[str, Any]]:
    with open_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return None

        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        return result


def set_task_fields(db_path: Path, task_id: str, fields: dict[str, Any]) -> None:
    if not fields:
        return

    fields = dict(fields)
    fields["updated_at"] = utc_now_iso()

    keys = list(fields.keys())
    set_clause = ", ".join([f"{k} = ?" for k in keys])
    values = [fields[k] for k in keys]

    with open_conn(db_path) as conn:
        conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE task_id = ?",
            (*values, task_id),
        )
        conn.commit()


def claim_task_for_run(db_path: Path, task_id: str) -> bool:
    now = utc_now_iso()
    with open_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = ?, stage = ?, progress = ?, updated_at = ?, started_at = ?
            WHERE task_id = ? AND status = ?
            """,
            ("running", "starting", 0.05, now, now, task_id, "queued"),
        )
        conn.commit()
        return cur.rowcount == 1


def mark_running_tasks_as_queued(db_path: Path) -> int:
    now = utc_now_iso()
    with open_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = ?, stage = ?, updated_at = ?
            WHERE status = ?
            """,
            ("queued", "queued_after_restart", now, "running"),
        )
        conn.commit()
        return cur.rowcount


def create_asset(
    db_path: Path,
    *,
    asset_id: str,
    asset_type: str,
    path: str,
    metadata: dict[str, Any],
) -> None:
    now = utc_now_iso()

    with open_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO assets (asset_id, type, path, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                asset_type,
                path,
                json.dumps(metadata),
                now,
            ),
        )
        conn.commit()


def get_asset(db_path: Path, asset_id: str) -> Optional[dict[str, Any]]:
    with open_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM assets WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        if not row:
            return None

        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result


def count_tasks_by_status(db_path: Path, statuses: Iterable[str]) -> int:
    statuses_list = [str(s).strip() for s in list(statuses) if str(s).strip()]
    if not statuses_list:
        return 0

    placeholders = ",".join(["?"] * len(statuses_list))

    with open_conn(db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM tasks WHERE status IN ({placeholders})",
            tuple(statuses_list),
        ).fetchone()
        if not row:
            return 0
        return int(row["c"] or 0)


def list_tasks(
    db_path: Path,
    *,
    statuses: Optional[Iterable[str]] = None,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "created_at",
    order_dir: str = "ASC",
) -> list[dict[str, Any]]:
    limit = int(limit)
    offset = int(offset)
    if limit <= 0:
        return []

    limit = min(limit, 200)
    offset = max(0, offset)

    order_by = str(order_by or "").strip() or "created_at"
    order_dir = str(order_dir or "").strip().upper() or "ASC"

    allowed_order_by = {"created_at", "updated_at", "started_at", "finished_at"}
    if order_by not in allowed_order_by:
        raise ValueError(f"Invalid order_by: {order_by}")
    if order_dir not in {"ASC", "DESC"}:
        raise ValueError(f"Invalid order_dir: {order_dir}")

    params: list[Any] = []
    where_clause = ""

    statuses_list: list[str] = []
    if statuses is not None:
        statuses_list = [str(s).strip() for s in list(statuses) if str(s).strip()]
    if statuses_list:
        placeholders = ",".join(["?"] * len(statuses_list))
        where_clause = f" WHERE status IN ({placeholders})"
        params.extend(statuses_list)

    query = (
        "SELECT * FROM tasks"
        + where_clause
        + f" ORDER BY {order_by} {order_dir}"
        + " LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    with open_conn(db_path) as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        task = dict(row)
        task["request"] = json.loads(task.pop("request_json"))
        results.append(task)
    return results


def create_preset(
    db_path: Path,
    *,
    preset_id: str,
    name: str,
    request: dict[str, Any],
    thumbnail_asset_id: Optional[str],
) -> None:
    now = utc_now_iso()

    with open_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO presets (
              preset_id, name, request_json, thumbnail_asset_id,
              created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                preset_id,
                name,
                json.dumps(request),
                thumbnail_asset_id,
                now,
                now,
            ),
        )
        conn.commit()


def get_preset(db_path: Path, preset_id: str) -> Optional[dict[str, Any]]:
    with open_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM presets WHERE preset_id = ?",
            (preset_id,),
        ).fetchone()
        if not row:
            return None

        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        return result


def list_presets(
    db_path: Path,
    *,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "created_at",
    order_dir: str = "DESC",
) -> list[dict[str, Any]]:
    limit = int(limit)
    offset = int(offset)
    if limit <= 0:
        return []

    limit = min(limit, 200)
    offset = max(0, offset)

    order_by = str(order_by or "").strip() or "created_at"
    order_dir = str(order_dir or "").strip().upper() or "DESC"

    allowed_order_by = {"created_at", "updated_at", "name"}
    if order_by not in allowed_order_by:
        raise ValueError(f"Invalid order_by: {order_by}")
    if order_dir not in {"ASC", "DESC"}:
        raise ValueError(f"Invalid order_dir: {order_dir}")

    with open_conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM presets ORDER BY {order_by} {order_dir} LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        preset = dict(row)
        preset["request"] = json.loads(preset.pop("request_json"))
        results.append(preset)
    return results


def delete_preset(db_path: Path, preset_id: str) -> bool:
    with open_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM presets WHERE preset_id = ?",
            (preset_id,),
        )
        conn.commit()
        return cur.rowcount == 1
