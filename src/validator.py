"""校验领域事件信封的基础字段。

仓库原有 ``validate_event`` 行为保持不变（缺字段与 version 检查，返回
中文错误串列表）。新增的 :func:`validate_envelope` 在此之上严格校验枚举、
时间戳与负载类型，供事件存储强制使用。
"""

from datetime import datetime

from .contracts import AggregateType, EventType

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")


def validate_event(record: dict) -> list[str]:
    """原有宽松校验：仅检查必填字段与 version 正整数。"""
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")
    return errors


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt


def validate_envelope(record: dict) -> list[str]:
    """严格信封校验：宽松校验 + 枚举 + 带时区时间戳 + payload 字典。"""
    errors = validate_event(record)
    if errors:
        return errors

    if not isinstance(record["event_id"], str) or not record["event_id"].strip():
        errors.append("event_id 必须是非空字符串")
    if not isinstance(record["aggregate_id"], str) or not record["aggregate_id"].strip():
        errors.append("aggregate_id 必须是非空字符串")
    if not isinstance(record["summary"], str) or not record["summary"].strip():
        errors.append("summary 必须是非空字符串")

    try:
        EventType(record["event_type"])
    except ValueError:
        errors.append(f"未知 event_type：{record['event_type']}")
    try:
        AggregateType(record["aggregate_type"])
    except ValueError:
        errors.append(f"未知 aggregate_type：{record['aggregate_type']}")

    ts = record["occurred_at"]
    if not isinstance(ts, str) or _parse_ts(ts) is None:
        errors.append("occurred_at 必须是带时区偏移的 ISO 8601 时间")

    payload = record.get("payload", {})
    if payload is None:
        record["payload"] = {}
    elif not isinstance(payload, dict):
        errors.append("payload 必须是对象")

    return errors


def parse_timestamp(value: str) -> datetime:
    """解析带时区时间戳；非法时抛出 ValueError。"""
    dt = _parse_ts(value)
    if dt is None:
        raise ValueError(f"非法时间戳：{value!r}")
    return dt
