"""混合体能完赛裁决：领域稳定枚举。

新增事件必须沿用仓库信封（见 :class:`Envelope`），枚举值一旦在生产中
出现即视为契约，只许新增不许改写含义。
"""

from enum import StrEnum


class EventType(StrEnum):
    """领域事件类型。"""

    # 赛制与报名资料
    FORMAT_REGISTERED = "FORMAT_REGISTERED"
    WAVE_STARTED = "WAVE_STARTED"
    ENTRY_CONFIRMED = "ENTRY_CONFIRMED"              # 原有
    PARTNER_REPLACED = "PARTNER_REPLACED"
    DEVICE_REGISTERED = "DEVICE_REGISTERED"
    DEVICE_STATUS_RECORDED = "DEVICE_STATUS_RECORDED"
    # 证据接入与组装
    SPLIT_RECORDED = "SPLIT_RECORDED"                # 原有
    EVIDENCE_REJECTED = "EVIDENCE_REJECTED"
    SEGMENT_ASSEMBLED = "SEGMENT_ASSEMBLED"
    ANOMALY_FLAGGED = "ANOMALY_FLAGGED"              # 原有
    # 医疗
    MEDICAL_STOP_ISSUED = "MEDICAL_STOP_ISSUED"
    MEDICAL_RESUME_AUTHORIZED = "MEDICAL_RESUME_AUTHORIZED"
    # 裁判认定与申诉
    RULING_SIGNED = "RULING_SIGNED"                  # 原有
    APPEAL_FILED = "APPEAL_FILED"
    APPEAL_DECIDED = "APPEAL_DECIDED"
    # 成绩发布
    RESULT_REPUBLISHED = "RESULT_REPUBLISHED"        # 原有（初榜/改判后重发）
    RESULT_LOCKED = "RESULT_LOCKED"
    CERTIFICATE_ISSUED = "CERTIFICATE_ISSUED"
    AWARD_REROLLED = "AWARD_REROLLED"


class AggregateType(StrEnum):
    """聚合根类型。"""

    RACE_FORMAT = "race_format"
    COMPETITION_ENTRY = "competition_entry"
    SPLIT_EVIDENCE = "split_evidence"
    RESULT_RELEASE = "result_release"
    DEVICE = "device"
    REVIEW_CASE = "review_case"
    MEDICAL_HOLD = "medical_hold"
    RULING = "ruling"
    APPEAL = "appeal"
    AWARD = "award"
    CERTIFICATE = "certificate"


class EvidenceSource(StrEnum):
    """证据来源渠道。迟到或重复由接入层统一处理，与来源无关。"""

    TIMING_CHIP = "timing_chip"          # 计时芯片：计时门读数
    STATION_JUDGE = "station_judge"      # 项目裁判：固定项目完成确认
    APPARATUS_SENSOR = "apparatus_sensor"  # 器械传感器：如攀爬架力/计次


class SegmentKind(StrEnum):
    """环节种类：跑段与固定项目交替。"""

    RUN = "run"
    STATION = "station"


class GateRole(StrEnum):
    """计时门在环节序列中的角色。"""

    START = "start"
    TRANSITION = "transition"   # 跑段与项目之间的交替门
    FINISH = "finish"


class DeviceStatus(StrEnum):
    """器械状态。只有 NOMINAL 的器械读数可进入正常组装。"""

    NOMINAL = "nominal"
    SUSPECT = "suspect"
    FAULT = "fault"
    RETIRED = "retired"


class AnomalyKind(StrEnum):
    """校验异常种类。异常一律只进待审，不直接处罚。"""

    DUPLICATE = "duplicate"                 # 同一环节重复上报
    LATE = "late"                           # 迟到上报（仍按真实时间归位）
    OUT_OF_ORDER = "out_of_order"           # 时间顺序与环节序列冲突
    MISSED_STATION = "missed_station"       # 疑似漏站
    CROSS_LANE_DEVICE = "cross_lane_device"  # 读数来自相邻赛道/他人器械
    DEVICE_SUSPECT = "device_suspect"       # 器械状态非 NOMINAL 期间读数
    IMPOSSIBLE_PACE = "impossible_pace"     # 物理不可能速度
    ENTRY_MISMATCH = "entry_mismatch"       # 选手/分枪/赛道与报名不符


class RulingType(StrEnum):
    """裁判认定类型（由不同权责分别认定）。"""

    MISSED_STATION = "missed_station"           # 漏站
    EQUIPMENT_FAULT = "equipment_fault"         # 设备故障（赛事方原因）
    VOLUNTEER_MISDIRECTION = "volunteer_misdirection"  # 志愿者误导
    ATHLETE_VIOLATION = "athlete_violation"     # 运动员违规


class RulingDisposition(StrEnum):
    """认定处理结果。"""

    TIME_PENALTY = "time_penalty"        # 加时（仅违规适用）
    DISQUALIFICATION = "disqualification"
    ADJUSTED_TIME = "adjusted_time"      # 按规则修正计时（设备/误导/漏站补救）
    NO_FAULT = "no_fault"                # 不罚


class MedicalStatus(StrEnum):
    STOPPED = "stopped"
    RESUMED = "resumed"


class AppealStatus(StrEnum):
    FILED = "filed"
    UPHELD = "upheld"        # 申诉成立，原认定被推翻并改判
    REJECTED = "rejected"    # 申诉驳回，原认定维持


class ReleaseStage(StrEnum):
    """成绩发布阶段，三个阶段都保留对应规则与证据。"""

    PRELIMINARY = "preliminary"  # 初榜
    UNDER_APPEAL = "under_appeal"  # 申诉中
    OFFICIAL = "official"        # 正式成绩


# 只有下列裁判角色有权签署对应认定
RULING_AUTHORITY: dict[RulingType, str] = {
    RulingType.MISSED_STATION: "station_referee",
    RulingType.EQUIPMENT_FAULT: "technical_delegate",
    RulingType.VOLUNTEER_MISDIRECTION: "chief_course_judge",
    RulingType.ATHLETE_VIOLATION: "competition_jury",
}

# 设备故障与志愿者误导属赛事方原因，不允许给出处罚性处理
NON_PENALIZING_TYPES = frozenset(
    {RulingType.EQUIPMENT_FAULT, RulingType.VOLUNTEER_MISDIRECTION}
)

# 原 validate_event 使用的枚举（向后兼容）
LEGACY_EVENT_TYPES = frozenset(
    {
        EventType.ENTRY_CONFIRMED,
        EventType.SPLIT_RECORDED,
        EventType.ANOMALY_FLAGGED,
        EventType.RULING_SIGNED,
        EventType.RESULT_REPUBLISHED,
    }
)
LEGACY_AGGREGATE_TYPES = frozenset(
    {
        AggregateType.RACE_FORMAT,
        AggregateType.COMPETITION_ENTRY,
        AggregateType.SPLIT_EVIDENCE,
        AggregateType.RESULT_RELEASE,
    }
)
