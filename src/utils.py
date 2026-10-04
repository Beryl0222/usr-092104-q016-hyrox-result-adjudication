"""事件接入的通用工具：时间、标识与规范化哈希。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4


def now_iso() -> str:
    """当前 UTC 时间（秒精度 ISO 字符串）。"""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def canonical_json(value: object) -> str:
    """稳定序列化：键排序、无空白，保证哈希可跨进程复现。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: object) -> str:
    """对任意可 JSON 化的值计算 sha256 摘要。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def event_hash(record: dict) -> str:
    """单条事件的内容哈希（不含链字段 prev_hash/chain_hash）。"""
    content = {k: v for k, v in record.items() if k not in ("prev_hash", "chain_hash")}
    return digest(content)
