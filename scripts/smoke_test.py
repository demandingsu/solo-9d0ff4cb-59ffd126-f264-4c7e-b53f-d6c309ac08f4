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

    print("\n全部冒烟检查通过 ✔")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000")
