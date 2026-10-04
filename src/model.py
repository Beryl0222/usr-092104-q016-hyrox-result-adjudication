"""混合体能完赛裁决的领域对象与摘要工具。

对象身份、事件顺序与版本语义的总体约定见 README 与 contracts/domain.schema.json。
本模块只放纯数据结构：裁决流程在 src/engine.py，环节合成在 src/assembly.py。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime


def parse_ts(value: str) -> datetime:
    """解析带时区的 ISO 时间；缺时区视为数据错误。"""
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError(f"时间必须携带时区：{value}")
    return ts


def canonical(obj: object) -> str:
    """公开结果验证用的确定性序列化（键排序、紧凑分隔、保留中文）。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(obj: object) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


# ---- 稳定词汇 ----

SEGMENT_RUN = "run"
SEGMENT_STATION = "station"
SEGMENT_KINDS = (SEGMENT_RUN, SEGMENT_STATION)

# 三类证据来源：计时芯片、项目裁判、器械传感器。
EVIDENCE_SOURCES = ("timing_chip", "station_referee", "apparatus_sensor")

EVIDENCE_ACCEPTED = "accepted"
EVIDENCE_LATE = "late"
EVIDENCE_DUPLICATE = "duplicate"
EVIDENCE_QUARANTINED = "quarantined"
EVIDENCE_EXCLUDED = "excluded"
EVIDENCE_REASSIGNED = "reassigned"

ANOMALY_KINDS = (
    "lane_mismatch",        # 记录赛道与报名赛道不一致（如相邻赛道器械成绩）
    "sequence_gap",         # 环节缺失，疑似漏站
    "time_inversion",       # 环节真实发生时间前后倒挂
    "unknown_segment",      # 环节序号超出赛制范围
    "apparatus_fault",      # 器械状态异常期间产生的传感器记录
    "partner_ineligible",   # 搭档资格未通过
    "late_partner_change",  # 分枪出发后变更搭档
    "external_report",      # 外部（裁判终端等）上报的待核事项
)
ANOMALY_PENDING = "pending"
ANOMALY_RESOLVED = "resolved"

# 四类只能由有权裁判分别认定的事项。
RULING_CATEGORIES = (
    "missed_station",
    "equipment_failure",
    "volunteer_misdirection",
    "athlete_violation",
)

RULING_AUTHORITY = {
    "missed_station": ("course_referee", "chief_referee"),
    "equipment_failure": ("equipment_referee", "chief_referee"),
    "volunteer_misdirection": ("course_referee", "chief_referee"),
    "athlete_violation": ("competition_referee", "chief_referee"),
}

RULING_OUTCOMES = (
    "confirm",            # 证据有效，维持原样（隔离记录经认定采纳）
    "exclude_evidence",   # 证据排除，不计入分段
    "reassign_evidence",  # 证据改属其他报名者（如串道记录归还原主）
    "time_adjustment",    # 无责时间减免（设备故障、志愿者误导等）
    "penalty",            # 罚时（运动员违规等）
)

APPARATUS_STATUSES = ("ok", "faulty", "offline", "maintenance")

RELEASE_STATUSES = ("initial", "appeal_pending", "official")

STANDING_RANKED = "ranked"
STANDING_PENDING = "pending_review"
STANDING_MEDICAL = "medical_hold"
STANDING_INCOMPLETE = "incomplete"


# ---- 数据结构 ----

@dataclass(frozen=True)
class Segment:
    index: int
    kind: str
    station_id: str | None = None


@dataclass
class RaceFormat:
    format_id: str
    version: int
    segments: list[Segment]
    groups: list[str]
    waves: dict[str, datetime]  # 分枪号 -> 出发时间
    rules: dict


@dataclass
class AppliedPenalty:
    ruling_id: str
    category: str
    reason: str
    seconds: int
    referee_role: str
    signed_at: str


@dataclass
class Entry:
    entry_id: str
    format_id: str
    version: int
    athlete_ids: tuple[str, ...]
    is_team: bool
    group: str
    wave: str
    lane: str
    partner_eligibility: dict
    confirmed_at: datetime
    penalties: list[AppliedPenalty] = field(default_factory=list)
    time_credit_seconds: int = 0
    medical_intervals: list[dict] = field(default_factory=list)
    medical_hold: bool = False


@dataclass
class Evidence:
    evidence_id: str
    entry_id: str
    lane: str
    segment_index: int
    source: str
    occurred_at: datetime   # 真实发生时间（信封 occurred_at）
    reported_at: datetime   # 上报到达时间
    device_seq: str | None
    metrics: dict
    status: str = EVIDENCE_ACCEPTED


@dataclass
class Split:
    """一个（报名者, 环节）至多一条分段；迟到/重复上报只追加提示。"""
    entry_id: str
    segment_index: int
    evidence_ids: list[str] = field(default_factory=list)
    recorded_at: datetime | None = None
    notices: list[str] = field(default_factory=list)


@dataclass
class Anomaly:
    anomaly_id: str
    entry_id: str
    kind: str
    detail: str
    evidence_ids: tuple[str, ...]
    raised_at: datetime
    segment_index: int | None = None
    status: str = ANOMALY_PENDING
    resolved_by: str | None = None  # 认定编号，或 auto:evidence（证据补齐自愈）


@dataclass
class Ruling:
    ruling_id: str
    category: str
    referee_id: str
    referee_role: str
    entry_id: str
    anomaly_ids: tuple[str, ...]
    outcome: str
    reason: str
    signed_at: datetime
    penalty_seconds: int = 0
    credit_seconds: int = 0
    reassign_to: str | None = None


@dataclass
class ApparatusStatus:
    apparatus_id: str
    lane: str
    status: str
    updated_at: datetime
    note: str = ""


@dataclass
class MedicalCase:
    entry_id: str
    stopped_at: datetime
    reason: str
    status: str = "active"  # active | returned
    returned_at: datetime | None = None
    authorized_by: str | None = None


@dataclass(frozen=True)
class FreezeSnapshot:
    """随每份成绩冻结的上下文：赛制版本、组别、分枪、搭档资格、器械状态。"""
    format_id: str
    format_version: int
    group: str
    wave: str
    entry_version: int
    athlete_ids: tuple[str, ...]
    partner_eligibility: dict
    apparatus: dict


@dataclass
class SplitView:
    segment_index: int
    kind: str
    station_id: str | None
    duration_seconds: float | None
    recorded_at: str | None
    evidence_ids: list[str]
    sources: list[str]
    disputed: bool
    notices: list[str]


@dataclass
class PenaltyView:
    ruling_id: str
    category: str
    reason: str
    seconds: int
    referee_role: str


@dataclass
class EntryResult:
    entry_id: str
    standing: str
    rank: int | None
    total_seconds: float | None
    penalty_seconds: int
    time_credit_seconds: int
    penalties: list[PenaltyView]
    splits: list[SplitView]
    freeze: FreezeSnapshot
    digest: str = ""


@dataclass
class Release:
    release_id: str
    version: int
    status: str
    published_at: datetime
    rules: dict
    results: list[EntryResult]
    previous_digest: str | None
    digest: str = ""


@dataclass
class Certificate:
    certificate_id: str
    release_id: str
    release_version: int
    entry_id: str
    rank: int
    group: str
    total_seconds: float
    result_digest: str
    status: str = "valid"  # valid | superseded
    superseded_by: str | None = None


# ---- 公开载荷与验证 ----

def split_view_payload(view: SplitView) -> dict:
    return {
        "segment_index": view.segment_index,
        "kind": view.kind,
        "station_id": view.station_id,
        "duration_seconds": view.duration_seconds,
        "recorded_at": view.recorded_at,
        "evidence_ids": list(view.evidence_ids),
        "sources": list(view.sources),
        "disputed": view.disputed,
        "notices": list(view.notices),
    }


def penalty_payload(penalty: PenaltyView) -> dict:
    return {
        "ruling_id": penalty.ruling_id,
        "category": penalty.category,
        "reason": penalty.reason,
        "seconds": penalty.seconds,
        "referee_role": penalty.referee_role,
    }


def freeze_payload(freeze: FreezeSnapshot) -> dict:
    return {
        "format_id": freeze.format_id,
        "format_version": freeze.format_version,
        "group": freeze.group,
        "wave": freeze.wave,
        "entry_version": freeze.entry_version,
        "athlete_ids": list(freeze.athlete_ids),
        "partner_eligibility": dict(freeze.partner_eligibility),
        "apparatus": dict(freeze.apparatus),
    }


def result_signing_payload(result: EntryResult) -> dict:
    return {
        "entry_id": result.entry_id,
        "standing": result.standing,
        "rank": result.rank,
        "total_seconds": result.total_seconds,
        "penalty_seconds": result.penalty_seconds,
        "time_credit_seconds": result.time_credit_seconds,
        "penalties": [penalty_payload(p) for p in result.penalties],
        "splits": [split_view_payload(s) for s in result.splits],
        "freeze": freeze_payload(result.freeze),
    }


def result_payload(result: EntryResult) -> dict:
    return {**result_signing_payload(result), "digest": result.digest}


def release_signing_payload(release: Release) -> dict:
    return {
        "release_id": release.release_id,
        "version": release.version,
        "status": release.status,
        "published_at": release.published_at.isoformat(),
        "rules": release.rules,
        "previous_digest": release.previous_digest,
        "results": [result_payload(r) for r in release.results],
    }


def release_payload(release: Release) -> dict:
    return {**release_signing_payload(release), "digest": release.digest}


def verify_release_payload(payload: dict) -> list[str]:
    """任何人拿到导出的发布载荷都可重算摘要验证；返回问题列表（空为通过）。"""
    if not isinstance(payload, dict) or "digest" not in payload:
        return ["发布载荷缺少 digest"]
    errors = []
    for result in payload.get("results", []):
        if "digest" not in result:
            errors.append(f"成绩缺少摘要：{result.get('entry_id')}")
            continue
        signing = {k: v for k, v in result.items() if k != "digest"}
        if digest_of(signing) != result["digest"]:
            errors.append(f"成绩摘要不符：{result.get('entry_id')}")
    signing = {k: v for k, v in payload.items() if k != "digest"}
    if digest_of(signing) != payload["digest"]:
        errors.append("发布摘要与内容不符")
    return errors
