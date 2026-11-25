#!/usr/bin/env python3
"""端到端冒烟脚本（仅标准库），可对任意环境（含 Docker 容器）运行：

    python scripts/smoke_test.py http://127.0.0.1:8000

依次验证：换日选最低云量总和、缺覆盖/超阈值原因、失败上传不改现役数据、
清单更新后旧交付仍可读。退出码非 0 表示失败。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(method: str, base: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def rect(lo, la, w=2.0, h=2.0):
    return {"min_lon": lo, "min_lat": la, "max_lon": lo + w, "max_lat": la + h}


def parcel(uid, lo=100.0, la=30.0):
    return {"parcel_uid": uid, "min_lon": lo, "min_lat": la,
            "max_lon": lo + 1.0, "max_lat": la + 1.0}


def scene(no, date, lo, la, cloud, line, w=2.0, h=2.0):
    return {"scene_no": no, "observation_date": date, **rect(lo, la, w, h),
            "cloud_cover": cloud, "source_line": line}


def check(name: str, cond: bool, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        raise SystemExit(1)


def main(base: str) -> None:
    status, body = call("GET", base, "/health")
    check("健康检查 /health", status == 200 and body["status"] == "ok")

    # --- 1. 换日：两天都可行，选云量总和最低的 2026-05-02 ---
    payload1 = {"cloud_threshold": 50,
                "parcels": [parcel("P1"), parcel("P2", lo=102.0)],
                "scenes": [
                    scene("S101", "2026-05-01", 99, 29, 30, 2),
                    scene("S102", "2026-05-01", 101, 29, 30, 3),
                    scene("S201", "2026-05-02", 99, 29, 15, 5),
                    scene("S202", "2026-05-02", 101, 29, 15, 6)]}
    status, up1 = call("POST", base, "/uploads", payload1)
    check("首次上传 201", status == 201, str(up1))
    status, d1 = call("POST", base, "/deliveries")
    check("换日：选 2026-05-02（总和 30 < 60）",
          status == 201 and d1["status"] == "feasible"
          and d1["selected_date"] == "2026-05-02"
          and d1["total_cloud_cover"] == 30.0,
          f"-> {d1.get('selected_date')} / {d1.get('total_cloud_cover')}")
    check("保留场景号与原始来源行号",
          {s["parcel_uid"]: (s["scene_no"], s["source_line"])
           for s in d1["selected_scenes"]} == {"P1": ("S201", 5), "P2": ("S202", 6)})

    # --- 2. 缺覆盖 / 超阈值：重新上传不可行清单 ---
    payload2 = {"cloud_threshold": 50,
                "parcels": [parcel("P1"), parcel("P2", lo=102.0)],
                "scenes": [
                    scene("S101", "2026-05-01", 99, 29, 10, 2, w=2, h=2),   # 不覆盖 P2
                    scene("S201", "2026-05-02", 99, 29, 80, 5, w=5, h=5)]}  # 全覆盖但 80% 超阈值
    status, up2 = call("POST", base, "/uploads", payload2)
    check("第二次上传 201", status == 201)
    status, d2 = call("POST", base, "/deliveries")
    reasons = {(r["observation_date"], r["parcel_uid"]): r["reason"]
               for r in d2["infeasible_reasons"]}
    check("缺覆盖：infeasible 且原因齐全",
          status == 201 and d2["status"] == "infeasible"
          and reasons.get(("2026-05-01", "P2")) == "uncovered"
          and reasons.get(("2026-05-02", "P2")) == "cloud_exceeded",
          f"-> {reasons}")

    # 旧交付仍可读（清单更新后快照不丢）
    status, old = call("GET", base, f"/deliveries/{d1['delivery_id']}")
    check("旧交付仍可读且内容不变",
          status == 200 and old["selected_date"] == "2026-05-02"
          and old["batch_id"] == up1["batch_id"])

    # --- 3. 失败上传：非法负载返回 422，现役数据不变 ---
    bad = {"cloud_threshold": 50, "parcels": [],
           "scenes": [scene("SX", "2026-05-03", 99, 29, 5, 9)]}
    status, _ = call("POST", base, "/uploads", bad)
    check("失败上传返回 422", status == 422)
    status, latest = call("GET", base, "/deliveries/latest")
    check("失败上传不改变现役批次/交付",
          status == 200 and latest["batch_id"] == up2["batch_id"]
          and latest["status"] == "infeasible")
    status, batches = call("GET", base, "/batches")
    check("失败上传未产生新批次（仍为 2 个）", status == 200 and len(batches) == 2)

    # --- 4. 同景认定：拆组再合并、撤销后仍相连、旧组号回溯 ---
    # 新批次：同一 UTC 观测日 4 条场景 A/B/C/D（行号 1~4）+ 异日场景 X（行号 5）
    payload3 = {"cloud_threshold": 50,
                "parcels": [parcel("P1")],
                "scenes": [
                    scene("A", "2026-06-01", 99, 29, 10, 1),
                    scene("B", "2026-06-01", 99, 29, 10, 2),
                    scene("C", "2026-06-01", 99, 29, 10, 3),
                    scene("D", "2026-06-01", 99, 29, 10, 4),
                    scene("X", "2026-06-02", 99, 29, 10, 5)]}
    status, up3 = call("POST", base, "/uploads", payload3)
    check("第三次上传 201", status == 201)
    b3 = up3["batch_id"]

    def assert_edge(action, line_a, line_b):
        return call("POST", base, "/same-scene-assertions", {
            "action": action,
            "first_scene": {"batch_id": b3, "source_line": line_a},
            "second_scene": {"batch_id": b3, "source_line": line_b}})

    # 4.1 链式登记 A-B、B-C：传递性使 A/B/C 同组，每次成员变化都发新号
    status, ab = assert_edge("assert", 1, 2)
    check("登记 A-B -> G1（新号）",
          status == 201 and ab["group_nos"] == ["G1"] and ab["groups"][0]["reused"] is False)

    def ids_of(group_no):
        """读取当前组的成员 scene_id 集合。"""
        st, gs = call("GET", base, "/scene-groups")
        assert st == 200
        for g in gs:
            if g["group_no"] == group_no:
                return {m["scene_id"] for m in g["members"]}
        return set()

    status, bc = assert_edge("assert", 2, 3)
    check("登记 B-C 传递合并 -> G2（新号）",
          status == 201 and bc["group_nos"] == ["G2"]
          and set(bc["groups"][0]["scene_ids"]) == ids_of("G2")
          and len(ids_of("G2")) == 3)
    # 冗余登记 A-C（已连通）：成员没变，沿用 G2
    status, ac = assert_edge("assert", 1, 3)
    check("冗余登记 A-C -> 沿用 G2",
          status == 201 and ac["group_nos"] == ["G2"] and ac["groups"][0]["reused"] is True)
    # 异日认定被拒（payload2 批次里 2026-05-02 的行号 5）
    status, cross_day = assert_edge("assert", 1, 5)
    check("同 UTC 观测日校验：异日 409", status == 409, str(cross_day))

    # 4.2 拆组：撤销 A-B。因 A-C 仍在，A/B/C 仍连通 -> 成员没变，沿用 G2
    status, revoke_ab = assert_edge("revoke", 1, 2)
    check("撤销 A-B 后仍经 A-C 相连 -> 沿用 G2",
          status == 201 and revoke_ab["group_nos"] == ["G2"]
          and revoke_ab["groups"][0]["reused"] is True)

    # 4.3 再撤 A-C 真正拆组：{A} 单独成组、{B,C} 一组，均发新号（号段不写死）
    status, revoke_ac = assert_edge("revoke", 1, 3)
    new_nos = set(revoke_ac["group_nos"])
    check("撤销 A-C 拆组 -> 单点组与二元组均发新号",
          status == 201 and len(new_nos) == 2 and "G2" not in new_nos
          and all(g["reused"] is False for g in revoke_ac["groups"]))
    status, groups_now = call("GET", base, "/scene-groups")
    current = {g["group_no"]: {m["scene_id"] for m in g["members"]} for g in groups_now}
    check("当前组恰为拆出的两个组（单点 + 二元），D 未参与认定不在任何组",
          status == 200 and set(current) == new_nos
          and sorted(len(ids) for ids in current.values()) == [1, 2])
    singleton_no = next(no for no in new_nos if len(current[no]) == 1)
    pair_no = next(no for no in new_nos if len(current[no]) == 2)
    g3, id_bc = current[singleton_no], current[pair_no]

    # 4.4 合并：重新登记 A-B，单点组与二元组合并为新组
    status, remerge = assert_edge("assert", 1, 2)
    merged_no = remerge["group_nos"][0]
    check("拆组后再合并 -> 发新号且含 A/B/C 三个成员",
          status == 201 and len(remerge["group_nos"]) == 1
          and merged_no not in new_nos and remerge["groups"][0]["reused"] is False
          and set(remerge["groups"][0]["scene_ids"]) == g3 | id_bc
          and len(remerge["groups"][0]["scene_ids"]) == 3)

    # 4.5 旧组号 G2 回溯：原成员 A/B/C 现全部在合并后的新组，现组只列一次
    status, trace = call("GET", base, "/scene-groups/by-old-no/G2")
    check("旧组号 G2 回溯：原成员齐全，现组只列一次且仅列原成员",
          status == 200
          and len(trace["original_members"]) == 3
          and [g["group_no"] for g in trace["current_groups"]] == [merged_no]
          and {m["scene_id"] for m in trace["current_groups"][0]["members"]} == g3 | id_bc)

    # 4.6 撤销不存在的关系 404，且不写入事件流
    status, before_events = call("GET", base, "/same-scene-assertions")
    n_events = len(before_events)
    status, _ = assert_edge("revoke", 3, 4)
    check("撤销不存在的认定 -> 404", status == 404)
    status, after_events = call("GET", base, "/same-scene-assertions")
    check("404 不产生事件写入", status == 200 and len(after_events) == n_events)

    print("\n全部冒烟检查通过 ✔")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000")
