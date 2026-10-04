"""把计时芯片、项目裁判与器械传感器记录合成为跑段与固定项目序列。

不变量：
- 每个（报名者, 环节）至多一条分段；迟到与重复上报只追加提示，不多算环节。
- 车道不符、序号越界的记录进入隔离区，等待有权裁判认定，不计入成绩。
- 校验异常只标记待审，不直接产生处罚。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.model import (
    EVIDENCE_DUPLICATE,
    EVIDENCE_LATE,
    EVIDENCE_QUARANTINED,
    Evidence,
    Split,
)


@dataclass
class AnomalyDraft:
    """合成过程发现的异常草稿，由引擎登记为正式异常并留痕。"""
    kind: str
    detail: str
    evidence_ids: tuple[str, ...]
    segment_index: int | None


class AssemblyBook:
    """同一赛制下所有报名者的环节合成表。"""

    def __init__(self, segment_count: int, grace_seconds: int):
        self.segment_count = segment_count
        self.grace_seconds = grace_seconds
        self._splits: dict[tuple[str, int], Split] = {}
        self._device_seen: set[tuple] = set()
        self._gap_flagged: set[tuple[str, int]] = set()

    def split(self, entry_id: str, segment_index: int) -> Split | None:
        return self._splits.get((entry_id, segment_index))

    def splits_for(self, entry_id: str) -> dict[int, Split]:
        return {
            index: split
            for (eid, index), split in self._splits.items()
            if eid == entry_id
        }

    def ingest(self, ev: Evidence, entry_lane: str) -> list[AnomalyDraft]:
        """按真实发生时间把一条记录归并入（报名者, 环节）。"""
        if not 0 <= ev.segment_index < self.segment_count:
            ev.status = EVIDENCE_QUARANTINED
            return [AnomalyDraft(
                "unknown_segment",
                f"环节序号 {ev.segment_index} 超出赛制范围",
                (ev.evidence_id,),
                ev.segment_index,
            )]
        if ev.lane != entry_lane:
            # 典型情形：相邻赛道的器械传感器记录被计到本报名者名下。
            ev.status = EVIDENCE_QUARANTINED
            return [AnomalyDraft(
                "lane_mismatch",
                f"记录赛道 {ev.lane} 与报名赛道 {entry_lane} 不一致",
                (ev.evidence_id,),
                ev.segment_index,
            )]
        split = self._splits.setdefault(
            (ev.entry_id, ev.segment_index), Split(ev.entry_id, ev.segment_index)
        )
        device_key = (ev.source, ev.lane, ev.segment_index, ev.device_seq)
        if ev.device_seq is not None:
            if device_key in self._device_seen:
                ev.status = EVIDENCE_DUPLICATE
                split.notices.append(f"重复上报 {ev.evidence_id}，未重复计段")
                return []
            self._device_seen.add(device_key)
        drafts: list[AnomalyDraft] = []
        if (ev.reported_at - ev.occurred_at).total_seconds() > self.grace_seconds:
            ev.status = EVIDENCE_LATE
            split.notices.append(f"迟到上报 {ev.evidence_id}，仍归并原环节")
        # 时间倒挂：与相邻环节的真实发生时间矛盾，记录保留但标记待审。
        prev_recorded = [
            s.recorded_at
            for (eid, idx), s in self._splits.items()
            if eid == ev.entry_id and idx < ev.segment_index and s.recorded_at is not None
        ]
        next_recorded = [
            s.recorded_at
            for (eid, idx), s in self._splits.items()
            if eid == ev.entry_id and idx > ev.segment_index and s.recorded_at is not None
        ]
        if prev_recorded and ev.occurred_at < max(prev_recorded):
            drafts.append(AnomalyDraft(
                "time_inversion",
                f"第 {ev.segment_index} 环节发生时间早于前序环节",
                (ev.evidence_id,),
                ev.segment_index,
            ))
        if next_recorded and ev.occurred_at > min(next_recorded):
            drafts.append(AnomalyDraft(
                "time_inversion",
                f"第 {ev.segment_index} 环节发生时间晚于后续环节",
                (ev.evidence_id,),
                ev.segment_index,
            ))
        split.evidence_ids.append(ev.evidence_id)
        split.recorded_at = (
            ev.occurred_at
            if split.recorded_at is None
            else max(split.recorded_at, ev.occurred_at)
        )
        # 漏环节检查：出现更靠后的环节而前序环节缺失，疑似漏站。
        for j in range(ev.segment_index):
            if (ev.entry_id, j) not in self._splits and (ev.entry_id, j) not in self._gap_flagged:
                self._gap_flagged.add((ev.entry_id, j))
                drafts.append(AnomalyDraft(
                    "sequence_gap",
                    f"第 {j} 环节缺失，疑似漏站",
                    (),
                    j,
                ))
        return drafts

    def force_accept(self, ev: Evidence) -> None:
        """经有权裁判认定后，把隔离记录强制归并入对应环节。"""
        split = self._splits.setdefault(
            (ev.entry_id, ev.segment_index), Split(ev.entry_id, ev.segment_index)
        )
        if ev.evidence_id not in split.evidence_ids:
            split.evidence_ids.append(ev.evidence_id)
        split.recorded_at = (
            ev.occurred_at
            if split.recorded_at is None
            else max(split.recorded_at, ev.occurred_at)
        )
        split.notices.append(f"{ev.evidence_id} 经认定采纳")
