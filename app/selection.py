"""核心选景逻辑。

规则（需求原文）：
1. 只有同一天（UTC 观测日）的场景能覆盖全部地块，且每块地块所选场景
   云量 <= 云量阈值，该日期才是可行日期。
2. 在所有可行日期中，按「各地块所选场景的云量总和」最低者入选；
   总和相同则取观测日最早的日期（确定性平局规则）。
3. 不可行日期要逐地块列出原因：未覆盖（uncovered）或超阈值（cloud_exceeded）。

覆盖判定：场景覆盖矩形完全包含地块矩形（边界相接也算覆盖）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


def covers(scene_min_lon: float, scene_min_lat: float,
           scene_max_lon: float, scene_max_lat: float,
           p_min_lon: float, p_min_lat: float,
           p_max_lon: float, p_max_lat: float) -> bool:
    """场景矩形是否完全包含地块矩形。"""
    return (scene_min_lon <= p_min_lon and scene_min_lat <= p_min_lat
            and scene_max_lon >= p_max_lon and scene_max_lat >= p_max_lat)


@dataclass
class Candidate:
    scene_id: int
    scene_no: str
    observation_date: str
    cloud_cover: float
    source_line: int
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float


def _best_scene_for_parcel(scenes: List[Candidate], parcel: Dict[str, Any]) -> Optional[Candidate]:
    """在同日内，返回覆盖该地块且云量最低的场景（平局取 scene_id 最小，保证确定性）。"""
    best: Optional[Candidate] = None
    for s in scenes:
        if covers(s.min_lon, s.min_lat, s.max_lon, s.max_lat,
                  parcel["min_lon"], parcel["min_lat"],
                  parcel["max_lon"], parcel["max_lat"]):
            if best is None or (s.cloud_cover, s.scene_id) < (best.cloud_cover, best.scene_id):
                best = s
    return best


def select_scenes(parcels: List[Dict[str, Any]],
                  scenes: List[Candidate],
                  cloud_threshold: float) -> Dict[str, Any]:
    """执行选景。

    返回:
      {
        "status": "feasible" | "infeasible",
        "selected_date": str | None,
        "total_cloud_cover": float | None,
        "selected": [(parcel_uid, Candidate), ...] | [],
        "reasons": [ {observation_date, parcel_uid, reason, detail}, ... ],
        "feasible_dates": [str, ...],
      }
    """
    by_date: Dict[str, List[Candidate]] = {}
    for s in scenes:
        by_date.setdefault(s.observation_date, []).append(s)

    feasible: List[Tuple[str, float, List[Tuple[str, Candidate]]]] = []
    reasons: List[Dict[str, str]] = []

    for date in sorted(by_date):
        day_scenes = by_date[date]
        day_total = 0.0
        day_selection: List[Tuple[str, Candidate]] = []
        day_ok = True

        for p in parcels:
            best = _best_scene_for_parcel(day_scenes, p)
            if best is None:
                day_ok = False
                reasons.append({
                    "observation_date": date,
                    "parcel_uid": p["parcel_uid"],
                    "reason": "uncovered",
                    "detail": "该日期没有任何候选场景的覆盖矩形包含此地块",
                })
                continue
            if best.cloud_cover > cloud_threshold:
                day_ok = False
                reasons.append({
                    "observation_date": date,
                    "parcel_uid": p["parcel_uid"],
                    "reason": "cloud_exceeded",
                    "detail": (f"覆盖此地块的最低云量场景 {best.scene_no} 云量 "
                               f"{best.cloud_cover:g}% 超过阈值 {cloud_threshold:g}%"),
                })
                continue
            day_total += best.cloud_cover
            day_selection.append((p["parcel_uid"], best))

        if day_ok:
            feasible.append((date, day_total, day_selection))

    if not feasible:
        return {
            "status": "infeasible",
            "selected_date": None,
            "total_cloud_cover": None,
            "selected": [],
            "reasons": reasons,
            "feasible_dates": [],
        }

    # 云量总和最低；平局取观测日最早（feasible 已按日期排序）
    best_date, best_total, best_selection = min(feasible, key=lambda x: (x[1], x[0]))
    return {
        "status": "feasible",
        "selected_date": best_date,
        "total_cloud_cover": best_total,
        "selected": best_selection,
        "reasons": reasons,
        "feasible_dates": [d for d, _, _ in feasible],
    }
