"""仅追加事件存储。

- 所有写入必须通过仓库信封（:func:`validate_envelope`）。
- ``event_id`` 全局幂等：迟到或重复上报携带同一 event_id 重放时不多记；
  若 event_id 相同但内容不同则判为冲突，拒绝写入。
- 每个聚合的 ``version`` 必须严格 +1，杜绝乱序覆盖。
- 全局事件以哈希链串联，公开结果可凭链摘要逐条验证。
"""

from __future__ import annotations

import json
from pathlib import Path

from .utils import canonical_json, digest
from .validator import validate_envelope


class EventConflictError(Exception):
    """同一 event_id 出现不同内容。"""


class EventValidationError(Exception):
    """事件未通过信封校验或版本冲突。"""


class EventStore:
    def __init__(self, journal: str | Path | None = None) -> None:
        self._events: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._journal = Path(journal) if journal else None
        if self._journal and self._journal.exists():
            for line in self._journal.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._ingest(json.loads(line), persist=False)

    # ---- 写入 -----------------------------------------------------------

    def append(self, record: dict) -> dict:
        """追加一条事件；重复 event_id 直接返回既有事件（幂等）。"""
        errors = validate_envelope(record)
        if errors:
            raise EventValidationError("；".join(errors))

        existing = self._by_id.get(record["event_id"])
        if existing is not None:
            if canonical_json(_hashable(existing)) != canonical_json(record):
                raise EventConflictError(f"event_id 重复但内容不一致：{record['event_id']}")
            return existing

        key = (record["aggregate_type"], record["aggregate_id"])
        expected = self._versions.get(key, 0) + 1
        if record["version"] != expected:
            raise EventValidationError(
                f"{key} 版本应为 {expected}，收到 {record['version']}"
            )

        stored = dict(record)
        stored.setdefault("payload", {})
        prev = self._events[-1]["chain_hash"] if self._events else None
        stored["prev_hash"] = prev
        stored["chain_hash"] = digest({"prev_hash": prev, "event": _hashable(stored)})

        self._ingest(stored, persist=True)
        return stored

    def _ingest(self, stored: dict, *, persist: bool) -> None:
        self._events.append(stored)
        self._by_id[stored["event_id"]] = stored
        key = (stored["aggregate_type"], stored["aggregate_id"])
        self._versions[key] = stored["version"]
        if persist and self._journal:
            with self._journal.open("a", encoding="utf-8") as fh:
                fh.write(canonical_json(stored) + "\n")

    # ---- 读取 -----------------------------------------------------------

    def events(self, aggregate_type: str | None = None, aggregate_id: str | None = None) -> list[dict]:
        result = self._events
        if aggregate_type is not None:
            result = [e for e in result if e["aggregate_type"] == aggregate_type]
        if aggregate_id is not None:
            result = [e for e in result if e["aggregate_id"] == aggregate_id]
        return list(result)

    def version_of(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions.get((aggregate_type, aggregate_id), 0)

    def contains(self, event_id: str) -> bool:
        return event_id in self._by_id

    @property
    def head_hash(self) -> str | None:
        return self._events[-1]["chain_hash"] if self._events else None

    def verify_chain(self) -> bool:
        """重算整条哈希链，供公开结果独立验证。"""
        prev = None
        for event in self._events:
            if event.get("prev_hash") != prev:
                return False
            if event["chain_hash"] != digest({"prev_hash": prev, "event": _hashable(event)}):
                return False
            prev = event["chain_hash"]
        return True


def _hashable(event: dict) -> dict:
    return {k: v for k, v in event.items() if k not in ("prev_hash", "chain_hash")}
