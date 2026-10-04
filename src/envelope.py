"""事件信封的类型枚举与配对约束，与 contracts/domain.schema.json 保持一致。

仓库已有信封字段（event_id、event_type、aggregate_type、aggregate_id、
occurred_at、version、summary）由 src.validator 校验基础形态；本模块在其上
补充稳定枚举与“事件-聚合”配对约束。新增事件只允许扩展枚举，不得修改既有取值。
"""

from src.validator import validate_event

EVENT_TYPES = (
    "ENTRY_CONFIRMED",
    "SPLIT_RECORDED",
    "ANOMALY_FLAGGED",
    "RULING_SIGNED",
    "RESULT_REPUBLISHED",
    "FORMAT_PUBLISHED",
    "APPARATUS_STATUS_CHANGED",
    "MEDICAL_STOPPED",
    "RETURN_AUTHORIZED",
)

AGGREGATE_TYPES = (
    "race_format",
    "competition_entry",
    "split_evidence",
    "result_release",
    "apparatus",
    "ruling",
)

# 每种事件允许挂载的聚合类型。
EVENT_AGGREGATE = {
    "FORMAT_PUBLISHED": ("race_format",),
    "ENTRY_CONFIRMED": ("competition_entry",),
    "SPLIT_RECORDED": ("split_evidence",),
    "ANOMALY_FLAGGED": ("competition_entry", "split_evidence"),
    "RULING_SIGNED": ("ruling",),
    "RESULT_REPUBLISHED": ("result_release",),
    "APPARATUS_STATUS_CHANGED": ("apparatus",),
    "MEDICAL_STOPPED": ("competition_entry",),
    "RETURN_AUTHORIZED": ("competition_entry",),
}


def validate_envelope(event: dict) -> list[str]:
    """校验信封基础字段、枚举取值与事件-聚合配对。"""
    errors = validate_event(event)
    event_type = event.get("event_type")
    aggregate_type = event.get("aggregate_type")
    if event_type is not None and event_type not in EVENT_TYPES:
        errors.append(f"未知事件类型：{event_type}")
    if aggregate_type is not None and aggregate_type not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{aggregate_type}")
    allowed = EVENT_AGGREGATE.get(event_type)
    if allowed and aggregate_type in AGGREGATE_TYPES and aggregate_type not in allowed:
        errors.append(f"事件 {event_type} 不允许挂在聚合 {aggregate_type}")
    if "payload" in event and not isinstance(event["payload"], dict):
        errors.append("payload 必须是对象")
    return errors
