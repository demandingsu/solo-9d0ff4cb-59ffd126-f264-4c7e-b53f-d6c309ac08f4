"""FastAPI 路由：上传清单、生成/查看选景交付、批次与历史查询。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import List

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from . import groups, schemas
from .database import get_conn, init_db
from .selection import Candidate, select_scenes


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="农业遥感同观测日选景 API",
    description=(
        "上传地块矩形范围与候选场景（场景号、UTC 观测日、覆盖矩形、云量、来源行号），"
        "选出同一天覆盖全部地块且每块云量不超过阈值的场景组合。"
        "支持批次版本化（清单更新后旧交付仍可读）与事务回滚（失败上传不改变现役数据）。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def db() -> sqlite3.Connection:
    # get_conn 是带 commit/rollback 的上下文管理器，依赖本身返回其管理的连接
    with get_conn() as conn:
        yield conn


# ---------------- 健康检查 / 文档 ----------------


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok"}


@app.exception_handler(sqlite3.Error)
def sqlite_error_handler(request, exc: sqlite3.Error):
    # get_conn 已在异常传播时回滚事务；此处仅把数据库错误转成规范 JSON
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=500,
                        content={"detail": f"数据库操作失败，事务已回滚: {exc}"})


# ---------------- 上传 ----------------


@app.post("/uploads", response_model=schemas.UploadResponse, status_code=201, tags=["清单管理"])
def upload(payload: schemas.UploadRequest, conn: sqlite3.Connection = Depends(db)):
    """上传一批地块与候选场景。

    - 整个上传在单个事务内完成：任一步失败则回滚，现役批次与数据保持不变。
    - 新批次生效时，旧批次标记为 superseded（数据与旧交付均保留，不删除）。
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO upload_batches (status, cloud_threshold, parcel_count, scene_count) "
        "VALUES ('active', ?, ?, ?)",
        (payload.cloud_threshold, len(payload.parcels), len(payload.scenes)),
    )
    batch_id = cur.lastrowid

    cur.executemany(
        "INSERT INTO parcels (batch_id, parcel_uid, min_lon, min_lat, max_lon, max_lat) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(batch_id, p.parcel_uid, p.min_lon, p.min_lat, p.max_lon, p.max_lat)
         for p in payload.parcels],
    )
    cur.executemany(
        "INSERT INTO scenes (batch_id, scene_no, observation_date, min_lon, min_lat, "
        "max_lon, max_lat, cloud_cover, source_line) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(batch_id, s.scene_no, s.observation_date, s.min_lon, s.min_lat,
          s.max_lon, s.max_lat, s.cloud_cover, s.source_line)
         for s in payload.scenes],
    )
    # 旧批次退役（仅改状态标记，行数据与交付快照全部保留）
    cur.execute(
        "UPDATE upload_batches SET status='superseded' WHERE id != ?", (batch_id,)
    )

    row = cur.execute(
        "SELECT id, created_at, cloud_threshold, parcel_count, scene_count "
        "FROM upload_batches WHERE id=?", (batch_id,)
    ).fetchone()
    return schemas.UploadResponse(
        batch_id=row["id"], created_at=row["created_at"],
        cloud_threshold=row["cloud_threshold"], parcel_count=row["parcel_count"],
        scene_count=row["scene_count"],
    )


# ---------------- 选景交付 ----------------


def _load_active_batches(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM upload_batches WHERE status='active' ORDER BY id"
    ).fetchall()


@app.post("/deliveries", response_model=schemas.DeliveryResponse, status_code=201, tags=["选景交付"])
def create_delivery(conn: sqlite3.Connection = Depends(db)):
    """基于现役批次执行选景，并把结果作为不可变交付快照保存。"""
    active = _load_active_batches(conn)
    if not active:
        raise HTTPException(409, "尚无现役清单，请先调用 POST /uploads 上传")
    batch = active[-1]
    batch_id = batch["id"]
    threshold = batch["cloud_threshold"]

    parcels = [dict(r) for r in conn.execute(
        "SELECT parcel_uid, min_lon, min_lat, max_lon, max_lat "
        "FROM parcels WHERE batch_id=? ORDER BY id", (batch_id,)
    ).fetchall()]
    scenes = [Candidate(**dict(r)) for r in conn.execute(
        "SELECT id AS scene_id, scene_no, observation_date, cloud_cover, source_line, "
        "min_lon, min_lat, max_lon, max_lat FROM scenes WHERE batch_id=?",
        (batch_id,),
    ).fetchall()]

    result = select_scenes(parcels, scenes, threshold)

    # 原因与可行日期一起序列化（__feasible_dates__ 为内部键，响应层单独提取）
    reasons_payload = list(result["reasons"])
    stored_json = json.dumps(
        {"reasons": reasons_payload, "__feasible_dates__": result["feasible_dates"]},
        ensure_ascii=False,
    )

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO deliveries (batch_id, status, selected_date, total_cloud_cover, "
        "cloud_threshold, parcel_count, selected_scene_count, reasons_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_id, result["status"], result["selected_date"], result["total_cloud_cover"],
         threshold, len(parcels),
         len(result["selected"]) if result["status"] == "feasible" else None,
         stored_json),
    )
    delivery_id = cur.lastrowid

    if result["status"] == "feasible":
        cur.executemany(
            "INSERT INTO delivery_scenes (delivery_id, parcel_uid, scene_id, scene_no, "
            "source_line, cloud_cover) VALUES (?, ?, ?, ?, ?, ?)",
            [(delivery_id, uid, c.scene_id, c.scene_no, c.source_line, c.cloud_cover)
             for uid, c in result["selected"]],
        )

    return _read_delivery(conn, delivery_id)


def _read_delivery(conn: sqlite3.Connection, delivery_id: int) -> schemas.DeliveryResponse:
    d = conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
    if d is None:
        raise HTTPException(404, f"交付不存在: delivery_id={delivery_id}")

    sel_rows = conn.execute(
        "SELECT ds.parcel_uid, ds.scene_no, ds.source_line, ds.cloud_cover, "
        "sc.observation_date FROM delivery_scenes ds "
        "JOIN scenes sc ON sc.id = ds.scene_id WHERE ds.delivery_id=? "
        "ORDER BY ds.parcel_uid",
        (delivery_id,),
    ).fetchall()
    stored = json.loads(d["reasons_json"])
    reasons = stored.get("reasons", [])
    feasible_dates = stored.get("__feasible_dates__", [])

    return schemas.DeliveryResponse(
        delivery_id=d["id"],
        batch_id=d["batch_id"],
        created_at=d["created_at"],
        status=d["status"],
        cloud_threshold=d["cloud_threshold"],
        selected_date=d["selected_date"],
        total_cloud_cover=d["total_cloud_cover"],
        parcel_count=d["parcel_count"],
        selected_scenes=[
            schemas.SelectedSceneOut(
                parcel_uid=r["parcel_uid"], scene_no=r["scene_no"],
                source_line=r["source_line"], cloud_cover=r["cloud_cover"],
                observation_date=r["observation_date"],
            ) for r in sel_rows
        ],
        infeasible_reasons=[schemas.InfeasibleReason(**r) for r in reasons],
        feasible_dates=feasible_dates,
    )


@app.get("/deliveries/latest", response_model=schemas.DeliveryResponse, tags=["选景交付"])
def latest_delivery(conn: sqlite3.Connection = Depends(db)):
    """查看现役批次的最新一次交付（清单更新后，旧交付仍可用 /deliveries/{id} 读取）。"""
    active = _load_active_batches(conn)
    if not active:
        raise HTTPException(409, "尚无现役清单")
    batch_id = active[-1]["id"]
    row = conn.execute(
        "SELECT id FROM deliveries WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "现役批次尚未生成交付，请先调用 POST /deliveries")
    return _read_delivery(conn, row["id"])


@app.get("/deliveries/{delivery_id}", response_model=schemas.DeliveryResponse, tags=["选景交付"])
def get_delivery(delivery_id: int, conn: sqlite3.Connection = Depends(db)):
    """按 ID 读取任意历史交付快照（含已被替代批次的旧交付）。"""
    return _read_delivery(conn, delivery_id)


@app.get("/deliveries", response_model=List[schemas.DeliverySummary], tags=["选景交付"])
def list_deliveries(conn: sqlite3.Connection = Depends(db)):
    """列出全部交付摘要（新的在前）。"""
    rows = conn.execute(
        "SELECT id, batch_id, created_at, status, selected_date, total_cloud_cover "
        "FROM deliveries ORDER BY id DESC"
    ).fetchall()
    return [schemas.DeliverySummary(
        delivery_id=r["id"], batch_id=r["batch_id"], created_at=r["created_at"],
        status=r["status"], selected_date=r["selected_date"],
        total_cloud_cover=r["total_cloud_cover"]) for r in rows]


# ---------------- 批次与明细查询 ----------------


@app.get("/batches", response_model=List[schemas.BatchSummary], tags=["清单管理"])
def list_batches(conn: sqlite3.Connection = Depends(db)):
    """列出全部上传批次（含已退役的 superseded 批次）。"""
    rows = conn.execute(
        "SELECT id, status, created_at, cloud_threshold, parcel_count, scene_count "
        "FROM upload_batches ORDER BY id DESC"
    ).fetchall()
    return [schemas.BatchSummary(
        batch_id=r["id"], status=r["status"], created_at=r["created_at"],
        cloud_threshold=r["cloud_threshold"], parcel_count=r["parcel_count"],
        scene_count=r["scene_count"]) for r in rows]


@app.get("/batches/{batch_id}/parcels", tags=["清单管理"])
def list_parcels(batch_id: int, conn: sqlite3.Connection = Depends(db)):
    if not conn.execute("SELECT 1 FROM upload_batches WHERE id=?", (batch_id,)).fetchone():
        raise HTTPException(404, f"批次不存在: batch_id={batch_id}")
    return [dict(r) for r in conn.execute(
        "SELECT parcel_uid, min_lon, min_lat, max_lon, max_lat "
        "FROM parcels WHERE batch_id=? ORDER BY id", (batch_id,)
    ).fetchall()]


@app.get("/batches/{batch_id}/scenes", tags=["清单管理"])
def list_scenes(batch_id: int, conn: sqlite3.Connection = Depends(db)):
    if not conn.execute("SELECT 1 FROM upload_batches WHERE id=?", (batch_id,)).fetchone():
        raise HTTPException(404, f"批次不存在: batch_id={batch_id}")
    return [dict(r) for r in conn.execute(
        "SELECT scene_no, observation_date, min_lon, min_lat, max_lon, max_lat, "
        "cloud_cover, source_line FROM scenes WHERE batch_id=? ORDER BY id",
        (batch_id,),
    ).fetchall()]


# ---------------- 同景认定与影像组 ----------------


@app.post(
    "/same-scene-assertions",
    response_model=schemas.SameSceneAssertionResponse,
    status_code=201,
    tags=["同景认定"],
)
def same_scene_assertion(
    payload: schemas.SameSceneAssertionRequest,
    conn: sqlite3.Connection = Depends(db),
):
    """登记或撤销两条场景的同景认定（按各自批次号 + 来源行号定位）。

    - 仅允许同一 UTC 观测日的两条场景进行认定，否则 409。
    - 同景认定可传递：认定关系构成无向图，连通分量即同一影像组。
    - 关系与组号在同一事务内生效：分量成员变化发新组号，成员没变沿用现号；
      任一步失败整体回滚，失败后查询仍是改动前的结果。
    - 撤销后不再与任何场景相连的参与场景会单独成组（同样发新号）。
    - 返回本次涉及场景当前所在的全部组号。
    """
    try:
        result = groups.apply_assertion(
            conn,
            payload.action,
            (payload.first_scene.batch_id, payload.first_scene.source_line),
            (payload.second_scene.batch_id, payload.second_scene.source_line),
        )
    except groups.GroupingError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return schemas.SameSceneAssertionResponse(**result)


@app.get(
    "/scene-groups",
    response_model=List[schemas.CurrentGroupOut],
    tags=["同景认定"],
)
def list_scene_groups(conn: sqlite3.Connection = Depends(db)):
    """列出全部当前影像组（只有参与过认定的场景才在组内）。"""
    return [schemas.CurrentGroupOut(**g) for g in groups.list_current_groups(conn)]


@app.get(
    "/scene-groups/{group_no}",
    response_model=schemas.CurrentGroupOut,
    tags=["同景认定"],
)
def get_scene_group(group_no: str, conn: sqlite3.Connection = Depends(db)):
    """按组号查看某个当前影像组的成员。"""
    try:
        result = groups.get_current_group(conn, group_no)
    except groups.GroupingError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return schemas.CurrentGroupOut(**result)


@app.get(
    "/scene-groups/by-old-no/{old_group_no}",
    response_model=schemas.OldGroupTraceResponse,
    tags=["同景认定"],
)
def trace_old_scene_group(old_group_no: str, conn: sqlite3.Connection = Depends(db)):
    """按旧组号找回原成员去向：每个仍含有原成员的现组列一次，并列出落在其中的原成员。"""
    try:
        result = groups.trace_old_group(conn, old_group_no)
    except groups.GroupingError as exc:
        raise HTTPException(exc.status_code, exc.detail)
    return schemas.OldGroupTraceResponse(**result)


@app.get(
    "/same-scene-assertions",
    response_model=List[schemas.AssertionEventOut],
    tags=["同景认定"],
)
def list_same_scene_assertions(conn: sqlite3.Connection = Depends(db)):
    """列出同景认定事件流（append-only，含已撤销事件），便于核查与排障。"""
    return [schemas.AssertionEventOut(**e) for e in groups.list_assertion_events(conn)]
