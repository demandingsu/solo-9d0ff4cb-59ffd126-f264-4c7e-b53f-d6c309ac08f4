"""请求/响应 Pydantic 模型与输入校验。"""
from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_CLOUD = 100.0


class Rect(BaseModel):
    """经纬度矩形范围（WGS84，十进制度）。"""

    min_lon: float = Field(..., ge=-180, le=180, description="最小经度（西边界）")
    min_lat: float = Field(..., ge=-90, le=90, description="最小纬度（南边界）")
    max_lon: float = Field(..., ge=-180, le=180, description="最大经度（东边界）")
    max_lat: float = Field(..., ge=-90, le=90, description="最大纬度（北边界）")

    @model_validator(mode="after")
    def _check_order(self) -> "Rect":
        if self.min_lon >= self.max_lon:
            raise ValueError("min_lon 必须小于 max_lon")
        if self.min_lat >= self.max_lat:
            raise ValueError("min_lat 必须小于 max_lat")
        return self


class ParcelIn(Rect):
    """上传地块：编号 + 矩形范围。"""

    parcel_uid: str = Field(..., min_length=1, max_length=128, description="地块编号")


class SceneIn(Rect):
    """候选场景：场景号、UTC 观测日、覆盖矩形、云量、来源行号。"""

    scene_no: str = Field(..., min_length=1, max_length=128, description="场景号")
    observation_date: str = Field(..., description="UTC 观测日，格式 YYYY-MM-DD")
    cloud_cover: float = Field(..., ge=0, le=MAX_CLOUD, description="云量百分比 0~100")
    source_line: int = Field(..., ge=1, description="来源行号（原始文件中的行号，从 1 开始）")

    @field_validator("observation_date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("observation_date 必须为 YYYY-MM-DD 格式")
        # 借助 datetime 验证真实日期（拒绝 2026-02-31 之类）
        from datetime import datetime

        datetime.strptime(v, "%Y-%m-%d")
        return v


class UploadRequest(BaseModel):
    cloud_threshold: float = Field(
        ..., ge=0, le=MAX_CLOUD, description="每块地块允许的最大云量（百分比，含边界）"
    )
    parcels: List[ParcelIn] = Field(..., min_length=1, description="地块列表（至少 1 块）")
    scenes: List[SceneIn] = Field(..., min_length=1, description="候选场景列表（至少 1 个）")

    @model_validator(mode="after")
    def _check_unique(self) -> "UploadRequest":
        parcel_ids = [p.parcel_uid for p in self.parcels]
        if len(set(parcel_ids)) != len(parcel_ids):
            dup = {x for x in parcel_ids if parcel_ids.count(x) > 1}
            raise ValueError(f"地块编号重复: {sorted(dup)}")
        lines = [s.source_line for s in self.scenes]
        if len(set(lines)) != len(lines):
            dup = sorted({x for x in lines if lines.count(x) > 1})
            raise ValueError(f"来源行号重复: {dup}")
        return self


# ---------- 响应模型 ----------


class SelectedSceneOut(BaseModel):
    parcel_uid: str
    scene_no: str
    source_line: int
    cloud_cover: float
    observation_date: str


class InfeasibleReason(BaseModel):
    observation_date: str
    parcel_uid: str
    reason: str  # uncovered | cloud_exceeded
    detail: str


class UploadResponse(BaseModel):
    batch_id: int
    created_at: str
    cloud_threshold: float
    parcel_count: int
    scene_count: int


class DeliveryResponse(BaseModel):
    delivery_id: int
    batch_id: int
    created_at: str
    status: str  # feasible | infeasible
    cloud_threshold: float
    selected_date: Optional[str]
    total_cloud_cover: Optional[float]
    parcel_count: int
    selected_scenes: List[SelectedSceneOut]
    infeasible_reasons: List[InfeasibleReason]
    feasible_dates: List[str]


class DeliverySummary(BaseModel):
    delivery_id: int
    batch_id: int
    created_at: str
    status: str
    selected_date: Optional[str]
    total_cloud_cover: Optional[float]


class BatchSummary(BaseModel):
    batch_id: int
    status: str
    created_at: str
    cloud_threshold: float
    parcel_count: int
    scene_count: int


# ---------- 同景认定 / 影像组 ----------


class SceneRef(BaseModel):
    """场景定位：批次号 + 来源行号。"""

    batch_id: int = Field(..., ge=1, description="场景所属上传批次号")
    source_line: int = Field(..., ge=1, description="场景在该批次中的原始来源行号")


class SameSceneAssertionRequest(BaseModel):
    action: str = Field(
        ..., description="assert=登记同景认定；revoke=撤销同景认定"
    )
    first_scene: SceneRef
    second_scene: SceneRef

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        if v not in ("assert", "revoke"):
            raise ValueError("action 只能是 assert 或 revoke")
        return v

    @model_validator(mode="after")
    def _check_distinct_refs(self) -> "SameSceneAssertionRequest":
        a, b = self.first_scene, self.second_scene
        if a.batch_id == b.batch_id and a.source_line == b.source_line:
            raise ValueError("first_scene 与 second_scene 不能是同一条场景")
        return self


class SceneIdentityOut(BaseModel):
    scene_id: int
    batch_id: int
    source_line: int
    scene_no: str
    observation_date: str


class AssertedGroupOut(BaseModel):
    group_no: str
    reused: bool = Field(..., description="true=成员未变沿用现号；false=成员变化发放新号")
    scene_ids: List[int]


class SameSceneAssertionResponse(BaseModel):
    action: str
    assertion_id: int
    first_scene: SceneIdentityOut
    second_scene: SceneIdentityOut
    group_nos: List[str] = Field(..., description="本次修改涉及场景当前所在的全部组号")
    groups: List[AssertedGroupOut]


class GroupMemberOut(SceneIdentityOut):
    pass


class CurrentGroupOut(BaseModel):
    group_no: str
    members: List[GroupMemberOut]


class OldGroupTraceResponse(BaseModel):
    old_group_no: str
    original_members: List[GroupMemberOut] = Field(..., description="旧组号快照中的原成员")
    current_groups: List[CurrentGroupOut] = Field(
        ..., description="每个仍含有原成员的现组列一次，members 仅列落在其中的原成员"
    )


class AssertionEventScene(BaseModel):
    batch_id: int
    source_line: int
    scene_no: str
    observation_date: str


class AssertionEventOut(BaseModel):
    assertion_id: int
    action: str
    created_at: str
    first_scene: AssertionEventScene
    second_scene: AssertionEventScene
