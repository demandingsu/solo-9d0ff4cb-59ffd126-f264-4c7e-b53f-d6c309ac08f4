"""自测用例（pytest）。

覆盖需求点名的三类场景：
  1. 换日：多个可行观测日时选云量总和最低的日期；改数据后结论随日期切换。
  2. 缺覆盖：某天无法覆盖全部地块时输出 uncovered / cloud_exceeded 原因，整体 infeasible。
  3. 失败上传：校验失败的上传不会写入、不改变现役批次与现役数据。
另含：来源行号保留、清单更新后旧交付仍可读（版本快照）。
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    os.environ["SCENE_DB_PATH"] = tmp.name
    # 延迟导入，确保环境变量生效
    from app import database
    database.init_db()
    from app.main import app
    with TestClient(app) as c:
        yield c
    os.unlink(tmp.name)
    os.environ.pop("SCENE_DB_PATH", None)


def rect(lo, la, hi_lon=None, hi_lat=None):
    """便捷构造矩形；默认矩形宽高各 1 度。"""
    return {"min_lon": lo, "min_lat": la,
            "max_lon": hi_lon if hi_lon is not None else lo + 1,
            "max_lat": hi_lat if hi_lat is not None else la + 1}


def parcel(uid, lo=100.0, la=30.0):
    return {"parcel_uid": uid, **rect(lo, la)}


def scene(no, date, lo, la, cloud, line, w=2.0, h=2.0):
    return {"scene_no": no, "observation_date": date,
            "min_lon": lo, "min_lat": la, "max_lon": lo + w, "max_lat": la + h,
            "cloud_cover": cloud, "source_line": line}


def upload(client, threshold, parcels, scenes):
    return client.post("/uploads", json={
        "cloud_threshold": threshold, "parcels": parcels, "scenes": scenes})


# ---------- 场景 1：换日 ----------


def test_switch_day_picks_lowest_total_cloud(client):
    """两个可行日期：2026-05-01 云量总和 60，2026-05-02 总和 30 -> 选 05-02。"""
    parcels = [parcel("P1"), parcel("P2", lo=102.0)]
    scenes = [
        # 05-01：两个大场景覆盖两块地，各 30%，总和 60
        scene("S101", "2026-05-01", 99, 29, 30, 1),
        scene("S102", "2026-05-01", 101, 29, 30, 2),
        # 05-02：同样覆盖，云量更低，总和 30
        scene("S201", "2026-05-02", 99, 29, 15, 3),
        scene("S202", "2026-05-02", 101, 29, 15, 4),
    ]
    r = upload(client, 50, parcels, scenes)
    assert r.status_code == 201, r.text

    d = client.post("/deliveries").json()
    assert d["status"] == "feasible"
    assert d["selected_date"] == "2026-05-02"
    assert d["total_cloud_cover"] == pytest.approx(30.0)
    assert d["feasible_dates"] == ["2026-05-01", "2026-05-02"]
    # 每块地一个场景，且保留场景号与原始来源行号
    assert {s["parcel_uid"]: (s["scene_no"], s["source_line"]) for s in d["selected_scenes"]} == {
        "P1": ("S201", 3), "P2": ("S202", 4)}


def test_switch_day_after_better_day_appears(client):
    """先只有高云量日期可行；重新上传加入低云量新日期后，结论切换到新日期。"""
    parcels = [parcel("P1")]
    r1 = upload(client, 50, parcels, [scene("S1", "2026-05-01", 99, 29, 40, 1)])
    d1 = client.post("/deliveries").json()
    assert d1["selected_date"] == "2026-05-01"

    # 清单更新：加入 05-02 的低云量场景
    r2 = upload(client, 50, parcels, [
        scene("S1", "2026-05-01", 99, 29, 40, 1),
        scene("S2", "2026-05-02", 99, 29, 5, 2),
    ])
    assert r2.status_code == 201
    d2 = client.post("/deliveries").json()
    assert d2["selected_date"] == "2026-05-02"
    assert d2["total_cloud_cover"] == pytest.approx(5.0)

    # 旧交付（delivery 1）仍可按 ID 读取，内容不变
    old = client.get(f"/deliveries/{d1['delivery_id']}").json()
    assert old["selected_date"] == "2026-05-01"
    assert old["selected_scenes"][0]["source_line"] == 1
    batches = client.get("/batches").json()
    assert {b["status"] for b in batches} == {"active", "superseded"}


def test_threshold_boundary_inclusive(client):
    """云量等于阈值（含边界）应当可行。"""
    r = upload(client, 20, [parcel("P1")], [scene("S1", "2026-05-01", 99, 29, 20, 1)])
    assert r.status_code == 201
    d = client.post("/deliveries").json()
    assert d["status"] == "feasible"
    assert d["total_cloud_cover"] == pytest.approx(20.0)


# ---------- 场景 2：缺覆盖 / 超阈值 ----------


def test_missing_coverage_reasons(client):
    """05-01 覆盖 P1 不覆盖 P2；05-02 全覆盖但 P2 云量超阈值 -> infeasible 且原因齐全。"""
    parcels = [parcel("P1"), parcel("P2", lo=102.0)]
    scenes = [
        # 05-01：只覆盖 P1（矩形止于 101）
        scene("S101", "2026-05-01", 99, 29, 10, 1, w=2, h=2),
        # 05-02：大场景全覆盖，但云量 80% 超阈值
        scene("S201", "2026-05-02", 99, 29, 80, 2, w=5, h=5),
    ]
    upload(client, 50, parcels, scenes)
    d = client.post("/deliveries").json()

    assert d["status"] == "infeasible"
    assert d["selected_date"] is None
    assert d["selected_scenes"] == []
    reasons = {(r["observation_date"], r["parcel_uid"]): r["reason"]
               for r in d["infeasible_reasons"]}
    assert reasons[("2026-05-01", "P2")] == "uncovered"
    assert reasons[("2026-05-02", "P1")] == "cloud_exceeded"
    assert reasons[("2026-05-02", "P2")] == "cloud_exceeded"
    assert d["feasible_dates"] == []

    # infeasible 交付也被持久化并可在列表中看到
    listed = client.get("/deliveries").json()
    assert listed[0]["status"] == "infeasible"


def test_per_parcel_lowest_cloud_scene_chosen(client):
    """同一天覆盖同一地块有多个场景时，选云量最低者。"""
    parcels = [parcel("P1")]
    scenes = [
        scene("S-HIGH", "2026-05-01", 99, 29, 45, 1),
        scene("S-LOW", "2026-05-01", 99, 29, 8, 2),
    ]
    upload(client, 50, parcels, scenes)
    d = client.post("/deliveries").json()
    assert d["status"] == "feasible"
    assert d["selected_scenes"][0]["scene_no"] == "S-LOW"
    assert d["selected_scenes"][0]["source_line"] == 2
    assert d["total_cloud_cover"] == pytest.approx(8.0)


# ---------- 场景 3：失败上传不改变现役数据 ----------


def test_failed_upload_keeps_active_data(client):
    # 先建立一份有效现役清单
    r = upload(client, 50, [parcel("P1")], [scene("S1", "2026-05-01", 99, 29, 10, 1)])
    assert r.status_code == 201
    first_batch = r.json()["batch_id"]

    bad_payloads = [
        # 空地块列表
        {"cloud_threshold": 50, "parcels": [], "scenes": [scene("S2", "2026-05-02", 99, 29, 5, 2)]},
        # 云量越界
        {"cloud_threshold": 50, "parcels": [parcel("P1")],
         "scenes": [scene("S2", "2026-05-02", 99, 29, 150, 2)]},
        # 来源行号重复
        {"cloud_threshold": 50, "parcels": [parcel("P1")],
         "scenes": [scene("S2", "2026-05-02", 99, 29, 5, 9),
                    scene("S3", "2026-05-02", 99, 29, 6, 9)]},
        # 非法日期
        {"cloud_threshold": 50, "parcels": [parcel("P1")],
         "scenes": [{"scene_no": "S2", "observation_date": "2026/05/02", **rect(99, 29),
                     "cloud_cover": 5, "source_line": 2}]},
        # 矩形边界反向
        {"cloud_threshold": 50, "parcels": [{"parcel_uid": "P1", "min_lon": 101, "min_lat": 30,
                                              "max_lon": 100, "max_lat": 31}],
         "scenes": [scene("S2", "2026-05-02", 99, 29, 5, 2)]},
        # 地块编号重复
        {"cloud_threshold": 50, "parcels": [parcel("P1"), parcel("P1")],
         "scenes": [scene("S2", "2026-05-02", 99, 29, 5, 2)]},
    ]
    for payload in bad_payloads:
        resp = client.post("/uploads", json=payload)
        assert resp.status_code == 422, payload

    # 现役批次未变、批次总数未增加
    batches = client.get("/batches").json()
    assert len(batches) == 1
    assert batches[0]["batch_id"] == first_batch
    assert batches[0]["status"] == "active"

    # 现役数据仍是原来的 1 地块 1 场景，交付结论不变
    assert len(client.get(f"/batches/{first_batch}/parcels").json()) == 1
    assert len(client.get(f"/batches/{first_batch}/scenes").json()) == 1
    d = client.post("/deliveries").json()
    assert d["batch_id"] == first_batch
    assert d["selected_scenes"][0]["scene_no"] == "S1"


def test_db_error_during_upload_rolls_back(client, monkeypatch):
    """模拟场景写入中途抛错：整个事务回滚，不留下半截批次，现役数据不变。"""
    import sqlite3
    from app import database

    r = upload(client, 50, [parcel("P1")], [scene("S1", "2026-05-01", 99, 29, 10, 1)])
    assert r.status_code == 201
    first_batch = r.json()["batch_id"]

    class FlakyCursor(sqlite3.Cursor):
        def executemany(self, sql, parameters):
            if "INSERT INTO scenes" in sql:
                raise sqlite3.OperationalError("simulated disk I/O error")
            return super().executemany(sql, parameters)

    class FlakyConnection(sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            kwargs.setdefault("factory", FlakyCursor)
            return super().cursor(*args, **kwargs)

    def flaky_connect(db_path):
        # 复刻 database._connect 的设置，仅替换连接工厂
        conn = sqlite3.connect(str(db_path), timeout=30, factory=FlakyConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(database, "_connect", flaky_connect)
    from app.main import app as fastapi_app
    with TestClient(fastapi_app, raise_server_exceptions=False) as err_client:
        resp = err_client.post("/uploads", json={
            "cloud_threshold": 50, "parcels": [parcel("P1")],
            "scenes": [scene("S9", "2026-05-03", 99, 29, 5, 7)]})
    assert resp.status_code == 500

    monkeypatch.undo()
    # 批次表没有新增行，旧批次仍为 active，数据完好
    batches = client.get("/batches").json()
    assert len(batches) == 1 and batches[0]["batch_id"] == first_batch
    assert client.get(f"/batches/{first_batch}/scenes").json()[0]["scene_no"] == "S1"


def test_no_active_batch_returns_409(client):
    assert client.post("/deliveries").status_code == 409
    assert client.get("/deliveries/latest").status_code == 409
