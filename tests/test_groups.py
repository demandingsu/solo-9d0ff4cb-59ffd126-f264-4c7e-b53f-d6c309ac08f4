"""同景认定 / 影像组自测（pytest）。

覆盖需求点名的规则：
  1. 按批次号 + 来源行号登记/撤销；只可认定同一 UTC 观测日；跨批次同日允许。
  2. 同景认定可传递（连通分量即同组）。
  3. 成员变化发新组号、成员没变沿用现号；撤销后孤立的参与场景单独成组。
  4. 修改返回涉及场景所在组号；旧组号可回溯原成员当前去向（每个现组列一次）。
  5. 关系与组号同一事务生效，失败回滚，失败后仍查到改动前结果。
  6. 从未参与认定的场景不在任何组内。
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
    from app import database
    database.init_db()
    from app.main import app
    with TestClient(app) as c:
        yield c
    os.unlink(tmp.name)
    os.environ.pop("SCENE_DB_PATH", None)


def rect(lo=99.0, la=29.0):
    return {"min_lon": lo, "min_lat": la, "max_lon": lo + 2.0, "max_lat": la + 2.0}


def scene(no, date, line, lo=99.0):
    return {"scene_no": no, "observation_date": date, **rect(lo),
            "cloud_cover": 10.0, "source_line": line}


def upload_day_scenes(client, lines_dates, threshold=50.0):
    """上传一个批次：1 块地块 + 指定 (行号, 观测日, 场景号) 的场景。"""
    scenes = [scene(no, date, line, lo=99.0 + line)
              for line, date, no in lines_dates]
    return client.post("/uploads", json={
        "cloud_threshold": threshold,
        "parcels": [{"parcel_uid": "P1", **rect(100.0, 30.0)}],
        "scenes": scenes,
    }).json()["batch_id"]


def assertion(client, action, a, b):
    return client.post("/same-scene-assertions", json={
        "action": action,
        "first_scene": {"batch_id": a[0], "source_line": a[1]},
        "second_scene": {"batch_id": b[0], "source_line": b[1]},
    })


def group_map(client):
    return {g["group_no"]: {m["scene_id"] for m in g["members"]}
            for g in client.get("/scene-groups").json()}


@pytest.fixture()
def four_scene_batch(client):
    """同观测日 4 条场景（行号 1~4：A/B/C/D）+ 另一观测日 1 条（行号 5：X）。"""
    batch_id = upload_day_scenes(client, [
        (1, "2026-06-01", "A"), (2, "2026-06-01", "B"),
        (3, "2026-06-01", "C"), (4, "2026-06-01", "D"),
        (5, "2026-06-02", "X"),
    ])
    return batch_id


def scene_id(client, batch_id, line):
    rows = client.get(f"/batches/{batch_id}/scenes").json()
    return next(i for i, r in enumerate(rows, start=1) if r["source_line"] == line)


# ---------- 1. 基本登记、同日校验、重复/撤销不存在 ----------


def test_assert_basic_and_same_day_rule(client, four_scene_batch):
    b = four_scene_batch
    r = assertion(client, "assert", (b, 1), (b, 2))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["action"] == "assert"
    assert body["group_nos"] == ["G1"]
    assert body["groups"][0]["reused"] is False
    assert set(body["groups"][0]["scene_ids"]) == {1, 2}
    # 响应保留两条场景各自的批次号与来源行号
    assert (body["first_scene"]["batch_id"], body["first_scene"]["source_line"]) == (b, 1)
    assert (body["second_scene"]["batch_id"], body["second_scene"]["source_line"]) == (b, 2)

    # 不同 UTC 观测日不可认定
    r2 = assertion(client, "assert", (b, 1), (b, 5))
    assert r2.status_code == 409
    assert "同一 UTC 观测日" in r2.json()["detail"]

    # 重复登记 -> 409
    assert assertion(client, "assert", (b, 2), (b, 1)).status_code == 409
    # 撤销不存在的关系 -> 404
    assert assertion(client, "revoke", (b, 3), (b, 4)).status_code == 404
    # 定位不到场景 -> 404
    assert assertion(client, "assert", (b, 99), (b, 1)).status_code == 404
    assert assertion(client, "assert", (999, 1), (b, 1)).status_code == 404
    # 入参校验 -> 422
    bad = client.post("/same-scene-assertions", json={
        "action": "freeze",
        "first_scene": {"batch_id": b, "source_line": 1},
        "second_scene": {"batch_id": b, "source_line": 2}})
    assert bad.status_code == 422
    same = client.post("/same-scene-assertions", json={
        "action": "assert",
        "first_scene": {"batch_id": b, "source_line": 1},
        "second_scene": {"batch_id": b, "source_line": 1}})
    assert same.status_code == 422

    # 当前组只有 G1，且未参与认定的 C/D/X 不在任何组内
    gm = group_map(client)
    assert gm == {"G1": {1, 2}}


# ---------- 2. 传递性：合并发新号，旧组号可回溯 ----------


def test_transitive_merge_new_number_and_trace(client, four_scene_batch):
    b = four_scene_batch
    assertion(client, "assert", (b, 1), (b, 2))
    r = assertion(client, "assert", (b, 2), (b, 3))
    assert r.status_code == 201
    body = r.json()
    # 三个场景合成一个新组 G2（成员变化，不复用 G1）
    assert body["group_nos"] == ["G2"]
    assert body["groups"][0]["reused"] is False
    assert set(body["groups"][0]["scene_ids"]) == {1, 2, 3}
    assert group_map(client) == {"G2": {1, 2, 3}}

    # 旧组号 G1 回溯：原成员 A/B 现在都落在 G2，现组只列一次且只列原成员
    trace = client.get("/scene-groups/by-old-no/G1").json()
    assert {m["scene_id"] for m in trace["original_members"]} == {1, 2}
    assert len(trace["current_groups"]) == 1
    cg = trace["current_groups"][0]
    assert cg["group_no"] == "G2"
    assert {m["scene_id"] for m in cg["members"]} == {1, 2}  # C 不是 G1 原成员，不列

    assert client.get("/scene-groups/by-old-no/G999").status_code == 404
    assert client.get("/scene-groups/G999").status_code == 404


# ---------- 3. 重复登记（已连通）成员没变 -> 沿用现号 ----------


def test_redundant_assert_reuses_number(client, four_scene_batch):
    b = four_scene_batch
    assertion(client, "assert", (b, 1), (b, 2))
    assertion(client, "assert", (b, 2), (b, 3))  # G2 = {A,B,C}
    r = assertion(client, "assert", (b, 1), (b, 3))  # 已连通，新增冗余边
    assert r.status_code == 201
    body = r.json()
    assert body["group_nos"] == ["G2"]
    assert body["groups"][0]["reused"] is True
    assert group_map(client) == {"G2": {1, 2, 3}}
    # 事件流保留三条 assert
    events = client.get("/same-scene-assertions").json()
    assert [e["action"] for e in events] == ["assert", "assert", "assert"]


# ---------- 4. 撤销拆组：发新号，孤立场景单独成组，旧号可追溯 ----------


def test_revoke_splits_into_new_groups_and_singleton(client, four_scene_batch):
    b = four_scene_batch
    assertion(client, "assert", (b, 1), (b, 2))  # G1 {A,B}
    assertion(client, "assert", (b, 2), (b, 3))  # G2 {A,B,C}

    r = assertion(client, "revoke", (b, 1), (b, 2))
    assert r.status_code == 201
    body = r.json()
    by_no = {g["group_no"]: g for g in body["groups"]}
    # 拆成 {A} 与 {B,C}，两个都发新号；A 孤立但单独成组
    assert set(body["group_nos"]) == {"G3", "G4"}
    assert all(g["reused"] is False for g in body["groups"])
    singleton = next(g for g in body["groups"] if len(g["scene_ids"]) == 1)
    assert singleton["scene_ids"] == [1]

    # 当前组视图：G1/G2 已成为历史，现组只有 G3/G4
    assert group_map(client) == {"G3": {1}, "G4": {2, 3}}

    # 旧组 G2 回溯：原成员 A/B/C 分别落在 G3、G4，每个现组列一次
    trace = client.get("/scene-groups/by-old-no/G2").json()
    assert {m["scene_id"] for m in trace["original_members"]} == {1, 2, 3}
    current = {g["group_no"]: {m["scene_id"] for m in g["members"]}
               for g in trace["current_groups"]}
    assert current == {"G3": {1}, "G4": {2, 3}}

    # 再撤销 B-C：B、C 都成为孤立单点，各自单独成组并再发新号
    r2 = assertion(client, "revoke", (b, 2), (b, 3))
    assert r2.status_code == 201
    assert set(r2.json()["group_nos"]) == {"G5", "G6"}
    assert group_map(client) == {"G3": {1}, "G5": {2}, "G6": {3}}
    # 撤销同一关系第二次 -> 404（事件流里已无有效边）
    assert assertion(client, "revoke", (b, 2), (b, 3)).status_code == 404


# ---------- 5. 撤销后仍相连 -> 成员没变，沿用现号 ----------


def test_revoke_still_connected_reuses_number(client, four_scene_batch):
    b = four_scene_batch
    assertion(client, "assert", (b, 1), (b, 2))  # G1
    assertion(client, "assert", (b, 2), (b, 3))  # G2 {A,B,C}
    assertion(client, "assert", (b, 1), (b, 3))  # 冗余边，沿用 G2
    # 撤销 A-B，但 A-C-B 仍连通，整组成员不变 -> 沿用 G2
    r = assertion(client, "revoke", (b, 1), (b, 2))
    assert r.status_code == 201
    assert r.json()["group_nos"] == ["G2"]
    assert r.json()["groups"][0]["reused"] is True
    assert group_map(client) == {"G2": {1, 2, 3}}

    # 再撤销 A-C 才真正拆开：{A} 与 {B,C} 均为新号
    r2 = assertion(client, "revoke", (b, 1), (b, 3))
    assert set(r2.json()["group_nos"]) == {"G3", "G4"}
    assert group_map(client) == {"G3": {1}, "G4": {2, 3}}


# ---------- 6. 跨批次：同日可认定，异日拒绝 ----------


def test_cross_batch_same_day_allowed(client, four_scene_batch):
    b1 = four_scene_batch
    b2 = upload_day_scenes(client, [(1, "2026-06-01", "E"), (2, "2026-06-03", "F")])
    r = assertion(client, "assert", (b1, 1), (b2, 1))
    assert r.status_code == 201, r.text
    assert r.json()["group_nos"] == ["G1"]

    # 跨批次但观测日不同 -> 409
    r2 = assertion(client, "assert", (b1, 1), (b2, 2))
    assert r2.status_code == 409


# ---------- 7. 失败回滚：关系与组号同事务，失败仍查到改动前结果 ----------


def test_failure_rolls_back_relation_and_groups(client, four_scene_batch, monkeypatch):
    import sqlite3
    from app import groups

    b = four_scene_batch
    assertion(client, "assert", (b, 1), (b, 2))  # G1
    before_groups = group_map(client)
    before_events = client.get("/same-scene-assertions").json()
    assert len(before_events) == 1

    # 模拟发号阶段数据库故障：事件写入也必须随事务回滚
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated disk I/O error")

    monkeypatch.setattr(groups, "_issue_group", boom)
    from app.main import app as fastapi_app
    with TestClient(fastapi_app, raise_server_exceptions=False) as err_client:
        resp = err_client.post("/same-scene-assertions", json={
            "action": "assert",
            "first_scene": {"batch_id": b, "source_line": 2},
            "second_scene": {"batch_id": b, "source_line": 3}})
    assert resp.status_code == 500
    monkeypatch.undo()

    # 改动前结果完好：仍只有 G1，事件流仍只有 1 条，C 未入组
    assert group_map(client) == before_groups
    events = client.get("/same-scene-assertions").json()
    assert len(events) == 1
    assert group_map(client) == {"G1": {1, 2}}

    # 业务校验失败（409/404）同样不产生任何写入
    assertion(client, "assert", (b, 1), (b, 5))  # 异日 409
    assertion(client, "revoke", (b, 3), (b, 4))  # 无边可撤 404
    assert len(client.get("/same-scene-assertions").json()) == 1
    assert group_map(client) == {"G1": {1, 2}}


# ---------- 8. 未参与认定的场景不进组；撤销后重新认定可再合并 ----------


def test_unasserted_scene_not_in_group_and_remerge(client, four_scene_batch):
    b = four_scene_batch
    assertion(client, "assert", (b, 1), (b, 2))
    assert client.get("/scene-groups").json()  # G1 存在
    listed = {m["scene_id"] for g in client.get("/scene-groups").json()
              for m in g["members"]}
    assert listed == {1, 2}  # 行号 3/4/5 的场景从未参与，不在任何组

    # 撤销后 A、B 各自单独成组
    r = assertion(client, "revoke", (b, 1), (b, 2))
    assert set(r.json()["group_nos"]) == {"G2", "G3"}
    # 重新登记：两个单组合并，成员集合为新组合，发新号 G4
    r2 = assertion(client, "assert", (b, 1), (b, 2))
    assert r2.json()["group_nos"] == ["G4"]
    assert r2.json()["groups"][0]["reused"] is False
    assert group_map(client) == {"G4": {1, 2}}

    # 单组明细可按组号读取，含批次号/来源行号/观测日
    detail = client.get("/scene-groups/G4").json()
    assert {m["source_line"] for m in detail["members"]} == {1, 2}
    assert all(m["observation_date"] == "2026-06-01" for m in detail["members"])
