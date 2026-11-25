"""SQLite 连接管理与建表语句。

- 所有写入均在单事务中完成，失败即回滚（失败上传不改变现役数据）。
- 采用 append-only 设计：任何上传都只新增数据、不更新/删除旧批次，
  因此旧交付（deliveries）始终可按其快照内容读取。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import get_db_path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS upload_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    status          TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    cloud_threshold REAL NOT NULL,
    parcel_count    INTEGER NOT NULL,
    scene_count     INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS parcels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   INTEGER NOT NULL REFERENCES upload_batches(id),
    parcel_uid TEXT NOT NULL,
    min_lon    REAL NOT NULL,
    min_lat    REAL NOT NULL,
    max_lon    REAL NOT NULL,
    max_lat    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parcels_batch ON parcels(batch_id);

CREATE TABLE IF NOT EXISTS scenes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL REFERENCES upload_batches(id),
    scene_no         TEXT NOT NULL,
    observation_date TEXT NOT NULL,           -- UTC 观测日，YYYY-MM-DD
    min_lon          REAL NOT NULL,
    min_lat          REAL NOT NULL,
    max_lon          REAL NOT NULL,
    max_lat          REAL NOT NULL,
    cloud_cover      REAL NOT NULL,
    source_line      INTEGER NOT NULL         -- 来源行号（原始文件中的行号）
);
CREATE INDEX IF NOT EXISTS idx_scenes_batch_date ON scenes(batch_id, observation_date);

-- 选景结果（交付快照）：每次生成都会新增一行，旧行永不修改。
CREATE TABLE IF NOT EXISTS deliveries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             INTEGER NOT NULL REFERENCES upload_batches(id),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    status               TEXT NOT NULL CHECK (status IN ('feasible', 'infeasible')),
    selected_date        TEXT,                -- 可行时的 UTC 观测日
    total_cloud_cover    REAL,                -- 可行时各地块所选场景云量总和
    cloud_threshold      REAL NOT NULL,
    parcel_count         INTEGER NOT NULL,
    selected_scene_count INTEGER,
    reasons_json         TEXT NOT NULL        -- 未覆盖/超阈值原因等明细（JSON）
);
CREATE INDEX IF NOT EXISTS idx_deliveries_batch ON deliveries(batch_id);

CREATE TABLE IF NOT EXISTS delivery_scenes (
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
    parcel_uid  TEXT NOT NULL,
    scene_id    INTEGER NOT NULL REFERENCES scenes(id),
    scene_no    TEXT NOT NULL,
    source_line INTEGER NOT NULL,             -- 保留原始来源行号
    cloud_cover REAL NOT NULL,
    PRIMARY KEY (delivery_id, parcel_uid)
);

-- 同景认定事件流（append-only）：一对场景的最后一条事件决定该关系当前是否有效。
-- scene_a_id/scene_b_id 规范化为较小/较大 scene_id，撤销只追加 revoke，不物理删除。
CREATE TABLE IF NOT EXISTS same_scene_assertions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_a_id  INTEGER NOT NULL REFERENCES scenes(id),
    scene_b_id  INTEGER NOT NULL REFERENCES scenes(id),
    action      TEXT NOT NULL CHECK (action IN ('assert', 'revoke')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_ssa_pair ON same_scene_assertions(scene_a_id, scene_b_id);

-- 影像组号：分量成员每次发生变化就发放新组号，并写入不可变成员快照，
-- 供数仓按旧组号回溯原成员当前去向。
CREATE TABLE IF NOT EXISTS scene_groups (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    group_no             TEXT NOT NULL UNIQUE,           -- 对外影像组号，如 G3
    source_assertion_id  INTEGER NOT NULL REFERENCES same_scene_assertions(id),
    change_action        TEXT NOT NULL CHECK (change_action IN ('assert', 'revoke')),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS scene_group_members (
    group_no  TEXT NOT NULL REFERENCES scene_groups(group_no),
    scene_id  INTEGER NOT NULL REFERENCES scenes(id),
    PRIMARY KEY (group_no, scene_id)
);
CREATE INDEX IF NOT EXISTS idx_sgm_scene ON scene_group_members(scene_id);

-- 场景当前所属组：仅参与过认定的场景存在映射；与认定事件在同一事务内替换，
-- 保证关系与组号一起生效，失败回滚后查到的仍是改动前的结果。
CREATE TABLE IF NOT EXISTS scene_group_current (
    scene_id  INTEGER PRIMARY KEY REFERENCES scenes(id),
    group_no  TEXT NOT NULL REFERENCES scene_groups(group_no)
);
"""


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """建库建表（幂等），容器启动与测试 fixture 均会调用。"""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """请求级连接：with 块正常退出自动 commit，抛异常自动 rollback。"""
    init_db()
    conn = _connect(get_db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
