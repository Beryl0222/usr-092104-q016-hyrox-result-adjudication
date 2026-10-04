"""混合体能完赛裁决后端（仅标准库）。

设计要点：

* 一切状态变更都是事件，写入 :class:`~src.store.EventStore`，必须符合仓库信封；
  迟到上报携带同一 event_id 重放由存储幂等处理，不重复计环节。
* 证据按 *真实发生时间*（observed_at）归位，接收时间（received_at）只用于
  判断迟到，不影响环节次序。
* 校验异常（错道、漏站、设备可疑、不可能速度等）只产生待审案件，绝不直接
  处罚；处罚只能来自有权裁判签署的认定。
* 医疗停止优先级高于计时流程；恢复参赛必须由另一授权角色批准。
* 每份成绩发布都冻结当时的赛制、组别、分枪、搭档资格与器械状态，并随附
  证据清单与快照哈希；改判后以新版本完成名次、证书与奖励递补。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from .contracts import (
    AggregateType,
    AnomalyKind,
    AppealStatus,
    DeviceStatus,
    EventType,
    NON_PENALIZING_TYPES,
    ReleaseStage,
    RULING_AUTHORITY,
    RulingDisposition,
    RulingType,
    SegmentKind,
)
from .projections import (
    AppealProjection,
    CaseProjection,
    DeviceProjection,
    EntryProjection,
    FormatProjection,
    MedicalProjection,
    RaceProjection,
    ReleaseProjection,
    RulingProjection,
    WaveProjection,
)
from .store import EventStore
from .utils import digest, new_id
from .validator import parse_timestamp

DEFAULT_MAX_SPEED_MPS = 12.0
LATE_GRACE_SECONDS = 60


class AdjudicationError(Exception):
    """业务规则被违反（无权、状态不允许、资料不一致等）。"""


class AdjudicationService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    # ================================================================
    # 内部投影（每次从事件流重建；赛事单机裁决规模下足够）
    # ================================================================

    def _snapshots(self) -> SimpleNamespace:
        events = self.store.events()
        return SimpleNamespace(
            formats=FormatProjection(events),
            entries=EntryProjection(events),
            devices=DeviceProjection(events),
            waves=WaveProjection(events),
            races=RaceProjection(events),
            medical=MedicalProjection(events),
            cases=CaseProjection(events),
            rulings=RulingProjection(events),
            appeals=AppealProjection(events),
            releases=ReleaseProjection(events),
        )

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str, payload: dict,
              occurred_at: str, summary: str, event_id: str | None = None) -> dict:
        version = self.store.version_of(aggregate_type, aggregate_id) + 1
        return self.store.append(
            {
                "event_id": event_id or new_id("ev"),
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": occurred_at,
                "version": version,
                "summary": summary,
                "payload": payload,
            }
        )

    # ================================================================
    # 赛制 / 分枪（规则随成绩冻结）
    # ================================================================

    def register_format(self, format_id: str, title: str, segments: list[dict], *,
                        rules_version: str, wave_ids: list[str] | None = None,
                        penalty_seconds: dict[str, int] | None = None,
                        max_speed_mps: float = DEFAULT_MAX_SPEED_MPS,
                        occurred_at: str, revision: int = 1) -> dict:
        """登记赛制。segments 必须是跑段/固定项目交替的 16 个环节。

        每个环节：{"code", "kind": run|station, "name", "distance_m"(跑段),
        "apparatus_code"(固定项目), "requires_sensor"(固定项目)}
        """
        if self.store.version_of(AggregateType.RACE_FORMAT, format_id):
            raise AdjudicationError(f"赛制已登记：{format_id}")
        if len(segments) != 16:
            raise AdjudicationError("赛制必须包含 16 个环节")
        for i, seg in enumerate(segments):
            expect = SegmentKind.RUN if i % 2 == 0 else SegmentKind.STATION
            if seg["kind"] != expect:
                raise AdjudicationError(f"环节 {seg['code']} 应为 {expect}，实际 {seg['kind']}")
        payload = {
            "title": title,
            "segments": segments,
            "rules_version": rules_version,
            "wave_ids": list(wave_ids or []),
            "penalty_seconds": penalty_seconds or {},
            "max_speed_mps": max_speed_mps,
            "revision": revision,
        }
        return self._emit(
            EventType.FORMAT_REGISTERED, AggregateType.RACE_FORMAT, format_id,
            payload, occurred_at, f"登记赛制 {title}", event_id=f"fmt-{format_id}",
        )

    def start_wave(self, format_id: str, wave_id: str, started_at: str) -> dict:
        snap = self._snapshots()
        snap.formats.get(format_id)
        return self._emit(
            EventType.WAVE_STARTED, AggregateType.RACE_FORMAT, format_id,
            {"wave_id": wave_id}, started_at, f"分枪 {wave_id} 发枪",
            event_id=f"wave-{format_id}-{wave_id}",
        )

    # ================================================================
    # 报名 / 双人组搭档替换（资格冻结在报名聚合上）
    # ================================================================

    def confirm_entry(self, entry_id: str, *, format_id: str, wave_id: str, category: str,
                      bib: str, athletes: list[dict], chip_ids: list[str], lane: str | None = None,
                      team_kind: str = "individual", device_ids: list[str] | None = None,
                      confirmed_at: str) -> dict:
        if team_kind == "pair" and len(athletes) != 2:
            raise AdjudicationError("双人组必须有两名运动员")
        payload = {
            "format_id": format_id, "wave_id": wave_id, "category": category,
            "team_kind": team_kind, "lane": lane, "bib": bib,
            "athletes": athletes, "chip_ids": chip_ids, "device_ids": device_ids or [],
        }
        return self._emit(
            EventType.ENTRY_CONFIRMED, AggregateType.COMPETITION_ENTRY, entry_id,
            payload, confirmed_at, f"确认报名 {bib}/{category}", event_id=f"entry-{entry_id}",
        )

    def replace_partner(self, entry_id: str, *, outgoing_athlete_id: str, incoming_athlete_id: str,
                        incoming_role: str, qualified: bool, registrar_id: str, reason: str,
                        decided_at: str) -> dict:
        """赛前替换搭档。资格条件：双人组、至多一次、发枪前、无任何证据、
        新搭档资格核验通过、未在同枪其他有效报名中。"""
        snap = self._snapshots()
        entry = snap.entries.get(entry_id)
        if entry is None:
            raise AdjudicationError(f"未知报名：{entry_id}")
        if entry["team_kind"] != "pair":
            raise AdjudicationError("仅双人组可替换搭档")
        if len(entry["replacements"]) >= 1:
            raise AdjudicationError("每个双人组最多替换一次搭档")
        if not any(a["athlete_id"] == outgoing_athlete_id for a in entry["athletes"]):
            raise AdjudicationError("被替换人不在当前搭档名单中")
        if not qualified:
            raise AdjudicationError("新搭档资格核验未通过，不得替换")
        gun = snap.waves.start(entry["format_id"], entry["wave_id"])
        moment = parse_timestamp(decided_at)
        if moment >= gun:
            raise AdjudicationError("搭档替换必须在发枪前完成")
        if snap.races.for_entry(entry_id)["splits"]:
            raise AdjudicationError("开赛后不得替换搭档")
        for other in snap.entries.entries.values():
            if other["entry_id"] == entry_id or other["wave_id"] != entry["wave_id"]:
                continue
            if any(a["athlete_id"] == incoming_athlete_id for a in other["athletes"]) \
                    and not any(r.get("outgoing_athlete_id") == incoming_athlete_id for r in other["replacements"]):
                raise AdjudicationError("新搭档已在同枪其他报名中")

        payload = {
            "outgoing_athlete_id": outgoing_athlete_id,
            "incoming_athlete_id": incoming_athlete_id,
            "incoming_role": incoming_role,
            "eligible": True,
            "qualified": True,
            "decided_by": registrar_id,
            "reason": reason,
        }
        return self._emit(
            EventType.PARTNER_REPLACED, AggregateType.COMPETITION_ENTRY, entry_id,
            payload, decided_at, f"{entry_id} 赛前替换搭档",
            event_id=f"partner-{entry_id}-{len(entry['replacements']) + 1}",
        )

    # ================================================================
    # 器械登记与状态（故障/恢复按真实时间成线）
    # ================================================================

    def register_device(self, device_id: str, *, apparatus_code: str, lane: str,
                        registered_at: str) -> dict:
        snap = self._snapshots()
        if device_id in snap.devices.devices:
            raise AdjudicationError(f"器械已登记：{device_id}")
        return self._emit(
            EventType.DEVICE_REGISTERED, AggregateType.DEVICE, device_id,
            {"apparatus_code": apparatus_code, "lane": lane},
            registered_at, f"登记器械 {apparatus_code} 赛道{lane}", event_id=f"dev-{device_id}",
        )

    def record_device_status(self, device_id: str, status: str, *, note: str,
                             recorded_at: str) -> dict:
        DeviceStatus(status)
        return self._emit(
            EventType.DEVICE_STATUS_RECORDED, AggregateType.DEVICE, device_id,
            {"status": status, "note": note}, recorded_at,
            f"器械 {device_id} 状态 {status}", event_id=new_id("devst"),
        )

    # ================================================================
    # 证据接入：迟到/重复不多算，异常只进待审
    # ================================================================

    def record_split(self, entry_id: str, *, source: str, observed_at: str,
                     received_at: str, event_id: str, reporter: str,
                     chip_id: str | None = None, gate_code: str | None = None,
                     segment_code: str | None = None, device_id: str | None = None,
                     reading: dict | None = None, bib: str | None = None,
                     value: float | None = None) -> dict:
        """接入一条证据。返回 SPLIT_RECORDED 或 EVIDENCE_REJECTED 事件。"""
        if self.store.contains(event_id):
            # 迟到/重复上报携带同一 event_id 重放：直接返回已存事件，不多算
            return next(e for e in self.store.events() if e["event_id"] == event_id)
        snap = self._snapshots()
        entry = snap.entries.get(entry_id)
        if entry is None:
            raise AdjudicationError(f"未知报名：{entry_id}")
        prior_rejected = next(
            (r["event_id"] for r in snap.races.for_entry(entry_id)["rejected"]
             if r.get("source_event_id") == event_id),
            None,
        )
        if prior_rejected is not None:
            # 此前已被判拒收的证据重放：原样返回，不再产生事件/案件
            return next(e for e in self.store.events() if e["event_id"] == prior_rejected)
        format_obj = snap.formats.get(entry["format_id"])
        observed = parse_timestamp(observed_at)

        split_payload = {
            "entry_id": entry_id,
            "format_id": entry["format_id"],
            "source": source,
            "observed_at": observed_at,
            "received_at": received_at,
            "reporter": reporter,
            "reading": reading or ({"value": value} if value is not None else {}),
        }
        if chip_id:
            split_payload["chip_id"] = chip_id
        if gate_code:
            split_payload["gate_code"] = gate_code
        if segment_code:
            split_payload["segment_code"] = segment_code
        if device_id:
            split_payload["device_id"] = device_id
        if bib:
            split_payload["bib"] = bib

        def reject(reason: str) -> dict:
            return self._emit(
                EventType.EVIDENCE_REJECTED, AggregateType.COMPETITION_ENTRY, entry_id,
                {**split_payload, "reason": reason, "source_event_id": event_id},
                received_at, f"证据拒收：{reason}", event_id=f"{event_id}-rejected",
            )

        # 医疗停止高于计时流程：停止期间一切读数不予组装
        hold = snap.medical
        if hold.is_stopped_at(entry_id, observed):
            return reject("medical_stop")

        # 上报号码布与报名不符
        if bib is not None and bib != entry["bib"]:
            self._flag(snap, entry_id, AnomalyKind.ENTRY_MISMATCH,
                       gate_code or segment_code or "", split_payload, observed_at,
                       detail=f"上报 bib={bib}，报名 bib={entry['bib']}")
            return reject("entry_mismatch")

        location = gate_code or segment_code or ""
        prior = snap.races.for_entry(entry_id)["splits"]

        if source == "timing_chip":
            if not gate_code or not chip_id:
                raise AdjudicationError("计时芯片证据必须带 chip_id 与 gate_code")
            if chip_id not in entry["chip_ids"]:
                self._flag(snap, entry_id, AnomalyKind.ENTRY_MISMATCH, location,
                           split_payload, observed_at, detail=f"芯片 {chip_id} 不属于 {entry_id}")
                return reject("chip_not_owned")
            dedup_key = ("timing_chip", chip_id, gate_code)
        elif source == "station_judge":
            if not segment_code:
                raise AdjudicationError("项目裁判证据必须带 segment_code")
            seg = format_obj["segments"][FormatProjection.segment_index(format_obj, segment_code)]
            if seg["kind"] != SegmentKind.STATION:
                raise AdjudicationError(f"{segment_code} 不是固定项目")
            dedup_key = ("station_judge", reporter, segment_code)
        elif source == "apparatus_sensor":
            if not segment_code or not device_id:
                raise AdjudicationError("器械传感器证据必须带 segment_code 与 device_id")
            if device_id not in snap.devices.devices:
                raise AdjudicationError(f"未知器械：{device_id}")
            device = snap.devices.get(device_id)
            # 相邻赛道器械成绩错记：器械绑定赛道与报名赛道不一致
            if entry["lane"] and device["lane"] != entry["lane"]:
                self._flag(snap, entry_id, AnomalyKind.CROSS_LANE_DEVICE, location,
                           split_payload, observed_at,
                           detail=f"器械 {device_id} 绑定赛道 {device['lane']}，"
                                  f"报名赛道 {entry['lane']}")
                return reject("cross_lane_device")
            status = snap.devices.status_at(device_id, observed)
            if status != DeviceStatus.NOMINAL:
                self._flag(snap, entry_id, AnomalyKind.DEVICE_SUSPECT, location,
                           split_payload, observed_at,
                           detail=f"器械 {device_id} 在读数时刻状态为 {status}")
                return reject("device_not_nominal")
            dedup_key = ("apparatus_sensor", device_id, segment_code)
        else:
            raise AdjudicationError(f"未知证据来源：{source}")

        # 重复上报：同一来源/同一位置已有读数 → 不多算环节，仅留待审痕迹
        dup = self._find_split(prior, dedup_key)
        if dup is not None:
            self._flag(snap, entry_id, AnomalyKind.DUPLICATE, location,
                       split_payload, observed_at,
                       detail=f"与已接证据 {dup['recorded_event']} 位置相同")
            return reject("duplicate")

        event = self._emit(
            EventType.SPLIT_RECORDED, AggregateType.COMPETITION_ENTRY, entry_id,
            split_payload, received_at, f"证据接入 {source}@{location}", event_id=event_id,
        )

        # 迟到上报：本场比赛已执行过组装，此证据在组装之后才到达 → 待审重排
        assembled_segments = snap.races.for_entry(entry_id)["assembled"]
        wait = parse_timestamp(received_at) - observed
        if assembled_segments and wait > timedelta(seconds=LATE_GRACE_SECONDS):
            last_assembly_at = max(
                parse_timestamp(a.get("assembled_at", a["ended_at"]))
                for a in assembled_segments
            )
            if parse_timestamp(received_at) > last_assembly_at:
                self._flag_late(entry_id, location, split_payload, observed_at, wait)
        return event

    @staticmethod
    def _find_split(prior: list[dict], dedup_key: tuple) -> dict | None:
        source, reporter_key, location = dedup_key
        for s in prior:
            if s["source"] != source:
                continue
            if source == "timing_chip" and s.get("chip_id") == reporter_key and s.get("gate_code") == location:
                return s
            if source == "station_judge" and s.get("reporter") == reporter_key and s.get("segment_code") == location:
                return s
            if source == "apparatus_sensor" and s.get("device_id") == reporter_key and s.get("segment_code") == location:
                return s
        return None

    def _flag_late(self, entry_id: str, location: str, payload: dict, observed_at: str,
                   wait: timedelta) -> None:
        # 重新取投影以包含刚写入的 split
        snap = self._snapshots()
        self._flag(snap, entry_id, AnomalyKind.LATE, location, payload, observed_at,
                   detail=f"延迟 {wait.total_seconds():.0f} 秒上报，需按真实时间重排")

    # ================================================================
    # 按真实时间组装 16 环节
    # ================================================================

    def assemble_race(self, entry_id: str, *, assembled_at: str | None = None) -> list[dict]:
        snap = self._snapshots()
        entry = snap.entries.get(entry_id)
        if entry is None:
            raise AdjudicationError(f"未知报名：{entry_id}")
        format_obj = snap.formats.get(entry["format_id"])
        race = snap.races.for_entry(entry_id)
        splits = race["splits"]

        # 计时门时刻：个人取本人最早通过；双人组须两人都过门，取较晚者
        gate_times: dict[str, datetime] = {}
        for s in splits:
            if s["source"] != "timing_chip":
                continue
            gate = s["gate_code"]
            moment = parse_timestamp(s["observed_at"])
            if gate not in gate_times:
                gate_times[gate] = moment
            else:
                gate_times[gate] = max(gate_times[gate], moment) if entry["team_kind"] == "pair" \
                    else min(gate_times[gate], moment)

        gates = [f"G{i}" for i in range(17)]

        # 证据必须连续：只组装到“第一个缺失计时门”为止，迟到门补齐后再续装
        covered_until = 0
        while covered_until < len(gates) and gates[covered_until] in gate_times:
            covered_until += 1
        covered_until -= 1

        # 单只芯片过门必须符合真实时间顺序：只在序号倒退（时间倒置）时挂异常；
        # 序号跳跃只说明中间门证据暂缺（可能迟到），由 covered_until 暂停组装
        gate_seq = {g: i for i, g in enumerate(gates)}
        for chip_id in entry["chip_ids"]:
            crossings = sorted(
                ((parse_timestamp(s["observed_at"]), s["gate_code"])
                 for s in splits
                 if s["source"] == "timing_chip" and s.get("chip_id") == chip_id),
                key=lambda x: x[0],
            )
            last = -1
            for moment, gate in crossings:
                idx = gate_seq.get(gate, -1)
                if idx <= last:
                    self._flag(snap, entry_id, AnomalyKind.OUT_OF_ORDER, gate,
                               {"chip_id": chip_id}, moment.isoformat(),
                               detail=f"芯片 {chip_id} 在 {moment.isoformat()} 通过 {gate}，"
                                      f"真实时间顺序与门序号冲突")
                last = idx

        events_out: list[dict] = []
        assembled_at = assembled_at or splits[-1]["received_at"] if splits else None
        existing = {s["segment_code"]: s for s in race["assembled"]}
        judge_stations = {s["segment_code"]: s for s in splits if s["source"] == "station_judge"}
        sensor_stations: dict[str, list[dict]] = {}
        for s in splits:
            if s["source"] == "apparatus_sensor":
                sensor_stations.setdefault(s["segment_code"], []).append(s)

        for i, seg in enumerate(format_obj["segments"]):
            if i >= covered_until:
                break  # 计时门不连续，缺口之后的环节一律不组装
            begin_gate, end_gate = gates[i], gates[i + 1]
            began, ended = gate_times[begin_gate], gate_times[end_gate]
            if ended < began:
                self._flag(snap, entry_id, AnomalyKind.OUT_OF_ORDER, seg["code"],
                           {"begin_gate": begin_gate, "end_gate": end_gate},
                           ended.isoformat(), detail="环节结束早于开始")
                continue

            evidence_ids, status, basis = [], "ok", "timing"
            if seg["kind"] == SegmentKind.RUN:
                evidence_ids = [s["recorded_event"] for s in splits
                                if s["source"] == "timing_chip"
                                and s.get("gate_code") in (begin_gate, end_gate)]
                distance = float(seg.get("distance_m", 0))
                seconds = (ended - began).total_seconds()
                speed = distance / seconds if distance and seconds > 0 else 0
                if speed > format_obj.get("max_speed_mps", DEFAULT_MAX_SPEED_MPS):
                    self._flag(snap, entry_id, AnomalyKind.IMPOSSIBLE_PACE, seg["code"],
                               {"speed_mps": round(speed, 2)}, ended.isoformat(),
                               detail=f"{seg['code']} 速度 {speed:.2f} m/s 超出物理上限")
                    status = "pending_review"
            else:
                window = (began, ended)
                judge = judge_stations.get(seg["code"])
                if judge is None:
                    self._flag(snap, entry_id, AnomalyKind.MISSED_STATION, seg["code"],
                               {"begin_gate": begin_gate, "end_gate": end_gate},
                               ended.isoformat(), detail="缺少项目裁判完成确认")
                    status = "pending_review"
                else:
                    jt = parse_timestamp(judge["observed_at"])
                    if not (window[0] <= jt <= window[1]):
                        self._flag(snap, entry_id, AnomalyKind.OUT_OF_ORDER, seg["code"],
                                   {"judge_event": judge["recorded_event"]},
                                   judge["observed_at"], detail="裁判确认时刻不在环节窗口内")
                        status = "pending_review"
                    evidence_ids.append(judge["recorded_event"])
                    basis = "timing+judge"
                sensors = sensor_stations.get(seg["code"], [])
                for sr in sensors:
                    evidence_ids.append(sr["recorded_event"])
                if seg.get("requires_sensor") and not sensors:
                    self._flag(snap, entry_id, AnomalyKind.MISSED_STATION, seg["code"],
                               {"missing": "apparatus_sensor"}, ended.isoformat(),
                               detail="缺少器械传感器完成记录")
                    status = "pending_review"
                elif sensors:
                    basis = "timing+judge+sensor" if judge else "timing+sensor"
                evidence_ids += [s["recorded_event"] for s in splits
                                 if s["source"] == "timing_chip"
                                 and s.get("gate_code") in (begin_gate, end_gate)]

            duration = (ended - began).total_seconds()
            payload = {
                "entry_id": entry_id,
                "format_id": entry["format_id"],
                "segment_code": seg["code"],
                "kind": seg["kind"],
                "began_at": began.isoformat(),
                "ended_at": ended.isoformat(),
                "duration_seconds": round(duration, 3),
                "status": status,
                "basis": basis,
                "evidence_ids": evidence_ids,
                "assembled_at": assembled_at or ended.isoformat(),
            }
            prior = existing.get(seg["code"])
            if prior is not None:
                prior_sig = {k: v for k, v in prior.items() if k not in ("event_id", "version", "supersedes")}
                if prior_sig == payload:
                    events_out.append(prior)
                    continue
                payload["supersedes"] = prior["event_id"]
            ev = self._emit(
                EventType.SEGMENT_ASSEMBLED, AggregateType.COMPETITION_ENTRY, entry_id,
                payload, assembled_at or ended.isoformat(),
                f"组装环节 {seg['code']}", event_id=new_id("seg"),
            )
            events_out.append(ev)
        return events_out

    # ================================================================
    # 待审案件
    # ================================================================

    def _case_id(self, snap: dict, entry_id: str, kind: AnomalyKind, location: str) -> str:
        base = f"case-{entry_id}-{kind}-{location}".lower()
        existing = snap.cases.cases.get(base)
        if existing is None or existing["resolution"] is None:
            return base
        seq = 2
        while f"{base}-{seq}" in snap.cases.cases and snap.cases.cases[f"{base}-{seq}"]["resolution"] is not None:
            seq += 1
        return f"{base}-{seq}"

    def _flag(self, snap: dict, entry_id: str, kind: AnomalyKind, location: str,
              evidence: dict, observed_at: str, *, detail: str) -> dict | None:
        case_id = self._case_id(snap, entry_id, kind, location)
        existing = snap.cases.cases.get(case_id)
        # 同一未决案件上的相同异常重复出现（如重放组装）不重复立案
        if existing is not None and existing["resolution"] is None and existing["anomalies"]:
            last = existing["anomalies"][-1]
            if last["kind"] == kind and last["detail"] == detail:
                return {"event_id": last["event_id"], "aggregate_id": case_id, "deduplicated": True}
        payload = {
            "entry_id": entry_id,
            "format_id": snap.entries.get(entry_id)["format_id"] if snap.entries.get(entry_id) else evidence.get("format_id"),
            "kind": kind,
            "location": location,
            "detail": detail,
            "observed_at": observed_at,
            "evidence_ref": evidence.get("recorded_event") or evidence.get("event_id"),
            "evidence": {k: v for k, v in evidence.items() if k in
                         ("source", "chip_id", "gate_code", "segment_code", "device_id", "reason", "reporter")},
        }
        return self._emit(
            EventType.ANOMALY_FLAGGED, AggregateType.REVIEW_CASE, case_id,
            payload, observed_at, f"待审：{kind} @ {location or entry_id}",
            event_id=new_id("anom"),
        )

    # ================================================================
    # 医疗停止 / 恢复授权
    # ================================================================

    def issue_medical_stop(self, entry_id: str, *, issued_by: str, reason: str,
                           issued_at: str, hold_id: str | None = None) -> dict:
        snap = self._snapshots()
        if snap.entries.get(entry_id) is None:
            raise AdjudicationError(f"未知报名：{entry_id}")
        for hold in snap.medical.holds.values():
            if hold["entry_id"] == entry_id and hold["status"] == "stopped":
                raise AdjudicationError("该选手已在医疗停止中")
        hold_id = hold_id or new_id("hold")
        return self._emit(
            EventType.MEDICAL_STOP_ISSUED, AggregateType.MEDICAL_HOLD, hold_id,
            {"entry_id": entry_id, "issued_by": issued_by, "reason": reason},
            issued_at, f"医疗停止 {entry_id}", event_id=f"stop-{hold_id}",
        )

    def authorize_resume(self, hold_id: str, *, authorized_by: str, authorized_at: str) -> dict:
        """恢复参赛另需授权：必须由不同于停止发起人的医疗授权人批准。"""
        snap = self._snapshots()
        hold = snap.medical.holds.get(hold_id)
        if hold is None:
            raise AdjudicationError(f"未知医疗停止：{hold_id}")
        if hold["status"] != "stopped":
            raise AdjudicationError("该医疗停止不在生效中")
        stream = self.store.events(AggregateType.MEDICAL_HOLD, hold_id)
        stop = next(e for e in stream if e["event_type"] == EventType.MEDICAL_STOP_ISSUED)
        if authorized_by == stop["payload"]["issued_by"]:
            raise AdjudicationError("恢复参赛必须由停止发起人之外的授权人批准")
        return self._emit(
            EventType.MEDICAL_RESUME_AUTHORIZED, AggregateType.MEDICAL_HOLD, hold_id,
            {"entry_id": hold["entry_id"], "authorized_by": authorized_by},
            authorized_at, f"恢复参赛 {hold['entry_id']}", event_id=f"resume-{hold_id}",
        )

    # ================================================================
    # 裁判认定：四类问题分别由有权裁判签署
    # ================================================================

    def sign_ruling(self, case_id: str, *, ruling_type: str, disposition: str,
                    judge_role: str, judge_id: str, rationale: str, signed_at: str,
                    penalty_seconds: int = 0, adjustment_seconds: int = 0,
                    supersedes_ruling_id: str | None = None, evidence_ids: list[str] | None = None,
                    ruling_id: str | None = None) -> dict:
        RulingType(ruling_type)
        RulingDisposition(disposition)
        required_role = RULING_AUTHORITY[RulingType(ruling_type)]
        if judge_role != required_role:
            raise AdjudicationError(f"{ruling_type} 只能由 {required_role} 认定（收到 {judge_role}）")

        snap = self._snapshots()
        case = snap.cases.cases.get(case_id)
        if case is None:
            raise AdjudicationError(f"未知待审案件：{case_id}")
        if case["resolution"] is not None and supersedes_ruling_id is None:
            raise AdjudicationError("案件已有认定；改判须通过申诉并注明被取代的认定")

        if ruling_type in NON_PENALIZING_TYPES and disposition in (
            RulingDisposition.TIME_PENALTY, RulingDisposition.DISQUALIFICATION
        ):
            raise AdjudicationError("设备故障与志愿者误导属赛事方原因，不得作出处罚性认定")
        if disposition == RulingDisposition.TIME_PENALTY and penalty_seconds <= 0:
            raise AdjudicationError("加时认定必须给出正数秒数")
        if disposition == RulingDisposition.ADJUSTED_TIME and adjustment_seconds == 0:
            raise AdjudicationError("修正计时认定必须给出调整秒数")

        anomaly = case["anomalies"][-1]
        ruling_id = ruling_id or new_id("ruling")
        payload = {
            "case_id": case_id,
            "entry_id": anomaly["entry_id"],
            "format_id": anomaly.get("format_id"),
            "ruling_type": ruling_type,
            "disposition": disposition,
            "penalty_seconds": penalty_seconds if disposition == RulingDisposition.TIME_PENALTY else 0,
            "adjustment_seconds": adjustment_seconds if disposition == RulingDisposition.ADJUSTED_TIME else 0,
            "rationale": rationale,
            "judge_role": judge_role,
            "signed_by": judge_id,
            "evidence_ids": evidence_ids or [a.get("evidence_ref") for a in case["anomalies"]],
        }
        if supersedes_ruling_id:
            payload["supersedes_ruling_id"] = supersedes_ruling_id
        return self._emit(
            EventType.RULING_SIGNED, AggregateType.RULING, ruling_id,
            payload, signed_at, f"认定 {ruling_type}/{disposition}", event_id=f"sign-{ruling_id}",
        )

    # ================================================================
    # 申诉
    # ================================================================

    def file_appeal(self, entry_id: str, ruling_id: str, *, grounds: str,
                    filed_by: str, filed_at: str,
                    within_seconds: int = 3600) -> dict:
        snap = self._snapshots()
        ruling = snap.rulings.rulings.get(ruling_id)
        if ruling is None or ruling["entry_id"] != entry_id:
            raise AdjudicationError("认定不存在或不属于该报名")
        deadline = parse_timestamp(ruling["signed_at"]) + timedelta(seconds=within_seconds)
        if parse_timestamp(filed_at) > deadline:
            raise AdjudicationError("已超过申诉时限")
        appeal_id = new_id("appeal")
        return self._emit(
            EventType.APPEAL_FILED, AggregateType.APPEAL, appeal_id,
            {"entry_id": entry_id, "ruling_id": ruling_id, "grounds": grounds, "filed_by": filed_by},
            filed_at, f"申诉 {entry_id}", event_id=f"file-{appeal_id}",
        )

    def decide_appeal(self, appeal_id: str, *, outcome: str, decided_by: str, decided_at: str,
                      note: str = "", new_ruling_id: str | None = None) -> dict:
        AppealStatus(outcome)
        snap = self._snapshots()
        appeal = snap.appeals.appeals.get(appeal_id)
        if appeal is None:
            raise AdjudicationError(f"未知申诉：{appeal_id}")
        if appeal["status"] != AppealStatus.FILED:
            raise AdjudicationError("申诉已裁决")
        if outcome == AppealStatus.UPHELD:
            if not new_ruling_id:
                raise AdjudicationError("申诉成立必须附改判认定")
            new_ruling = snap.rulings.rulings.get(new_ruling_id)
            if new_ruling is None or new_ruling.get("supersedes_ruling_id") != appeal["ruling_id"]:
                raise AdjudicationError("改判认定必须取代原认定")
        payload = {
            "entry_id": appeal["entry_id"],
            "ruling_id": appeal["ruling_id"],
            "outcome": outcome,
            "decided_by": decided_by,
            "note": note,
        }
        if new_ruling_id:
            payload["new_ruling_id"] = new_ruling_id
        return self._emit(
            EventType.APPEAL_DECIDED, AggregateType.APPEAL, appeal_id,
            payload, decided_at, f"申诉裁决 {outcome}", event_id=f"decide-{appeal_id}",
        )

    # ================================================================
    # 成绩计算 + 版本化发布（冻结规则与证据）
    # ================================================================

    def _effective_rulings(self, snap: dict, entry_id: str) -> list[dict]:
        """当前有效认定：被改判取代链指向的旧认定不再计罚。"""
        superseded = {
            r["supersedes_ruling_id"]
            for r in snap.rulings.rulings.values()
            if r.get("supersedes_ruling_id")
        }
        return [r for r in snap.rulings.for_entry(entry_id) if r["ruling_id"] not in superseded]

    def _score_entry(self, snap: dict, entry: dict) -> dict:
        entry_id = entry["entry_id"]
        race = snap.races.for_entry(entry_id)
        chips = [s for s in race["splits"] if s["source"] == "timing_chip"]
        gates: dict[str, datetime] = {}
        for s in chips:
            t = parse_timestamp(s["observed_at"])
            gate = s["gate_code"]
            if gate not in gates:
                gates[gate] = t
            elif entry["team_kind"] == "pair":
                gates[gate] = max(gates[gate], t)
            else:
                gates[gate] = min(gates[gate], t)

        start, finish = gates.get("G0"), gates.get("G16")
        assembled = {}
        for s in race["assembled"]:
            old = assembled.get(s["segment_code"])
            if old is None or s["version"] > old["version"]:
                assembled[s["segment_code"]] = s

        result: dict = {
            "entry_id": entry_id,
            "bib": entry["bib"],
            "category": entry["category"],
            "team_kind": entry["team_kind"],
            "athletes": entry["athletes"],
            "segments": [
                {k: assembled[seg["code"]][k] for k in
                 ("segment_code", "kind", "began_at", "ended_at", "duration_seconds", "status", "basis", "evidence_ids")
                 if seg["code"] in assembled}
                for seg in snap.formats.get(entry["format_id"])["segments"] if seg["code"] in assembled
            ],
            "open_case_ids": snap.cases.open_for_entry(entry_id),
            "under_appeal": snap.appeals.open_for_entry(entry_id),
            "penalties": [],
        }

        if start is None or finish is None:
            result.update(status="incomplete", final_seconds=None, rank=None)
            return result

        gross = (finish - start).total_seconds()
        medical_seconds = 0.0
        for hold in snap.medical.holds.values():
            if hold["entry_id"] != entry_id:
                continue
            for a, b in hold["intervals"]:
                medical_seconds += max(0.0, (min(b, finish) - max(a, start)).total_seconds())

        penalty_seconds, adjustment_seconds, dq = 0, 0, False
        for r in self._effective_rulings(snap, entry_id):
            result["penalties"].append(
                {"ruling_id": r["ruling_id"], "type": r["ruling_type"],
                 "disposition": r["disposition"], "seconds": r["penalty_seconds"],
                 "rationale": r["rationale"], "signed_by": r["signed_by"]}
            )
            penalty_seconds += r["penalty_seconds"]
            adjustment_seconds += r["adjustment_seconds"]
            if r["disposition"] == RulingDisposition.DISQUALIFICATION:
                dq = True

        result["gross_seconds"] = round(gross, 3)
        result["medical_seconds"] = round(medical_seconds, 3)
        result["penalty_seconds"] = penalty_seconds
        result["adjustment_seconds"] = adjustment_seconds
        result["final_seconds"] = round(gross - medical_seconds + penalty_seconds + adjustment_seconds, 3)
        result["status"] = "disqualified" if dq else "ranked"
        return result

    def _freeze(self, snap: dict, format_id: str, scored: list[dict]) -> dict:
        format_obj = snap.formats.get(format_id)
        entry_ids = {s["entry_id"] for s in scored}
        devices_used = {}
        for e in snap.entries.entries.values():
            if e["format_id"] != format_id:
                continue
            for d in e.get("device_ids", []):
                devices_used[d] = True
        freeze = {
            "rules": {
                "format_id": format_id,
                "rules_version": format_obj["rules_version"],
                "revision": format_obj["revision"],
                "segments": format_obj["segments"],
                "penalty_seconds": format_obj["penalty_seconds"],
                "segments_digest": digest(format_obj["segments"]),
            },
            "waves": {
                wave_id: snap.waves.start(format_id, wave_id).isoformat()
                for (fid, wave_id) in snap.waves.waves if fid == format_id
            },
            "entries": {},
            "devices": {},
            "evidence_manifest": {},
        }
        for entry_id in sorted(entry_ids):
            e = snap.entries.get(entry_id)
            freeze["entries"][entry_id] = {
                "bib": e["bib"], "category": e["category"], "wave_id": e["wave_id"],
                "lane": e["lane"], "team_kind": e["team_kind"],
                "athletes": e["athletes"], "chip_ids": e["chip_ids"],
                "device_ids": e["device_ids"],
                "partner_replacements": e["replacements"],
            }
            race = snap.races.for_entry(entry_id)
            freeze["evidence_manifest"][entry_id] = sorted(
                s["recorded_event"] for s in race["splits"]
            )
        for d in devices_used:
            dev = snap.devices.get(d)
            freeze["devices"][d] = {
                "apparatus_code": dev["apparatus_code"], "lane": dev["lane"],
                "status": dev["timeline"][-1]["status"] if dev["timeline"] else DeviceStatus.NOMINAL,
                "timeline_digest": digest([
                    {"at": t["at"].isoformat(), "status": t["status"], "note": t["note"]}
                    for t in dev["timeline"]
                ]),
            }
        return freeze

    def publish_results(self, format_id: str, *, stage: str, published_by: str,
                        published_at: str) -> dict:
        ReleaseStage(stage)
        snap = self._snapshots()
        format_obj = snap.formats.get(format_id)
        entries = [e for e in snap.entries.entries.values() if e["format_id"] == format_id]
        scored = [self._score_entry(snap, e) for e in entries]

        open_appeals = [s for s in scored if s["under_appeal"]]
        if stage == ReleaseStage.UNDER_APPEAL and not open_appeals:
            raise AdjudicationError("不存在申诉中的报名，不能发布申诉中状态")
        if stage == ReleaseStage.PRELIMINARY and open_appeals:
            raise AdjudicationError("已有申诉进行中，应发布申诉中状态")
        if stage == ReleaseStage.OFFICIAL:
            raise AdjudicationError("正式成绩须通过 lock_results 发布")

        ranked = sorted(
            (s for s in scored if s["status"] == "ranked"),
            key=lambda s: (s["category"], s["final_seconds"]),
        )
        rank_by_category: dict[str, int] = {}
        for s in ranked:
            rank_by_category[s["category"]] = rank_by_category.get(s["category"], 0) + 1
            s["rank"] = rank_by_category[s["category"]]
        for s in scored:
            if s["status"] != "ranked":
                s["rank"] = None

        freeze = self._freeze(snap, format_id, scored)
        release_id = f"release-{format_id}"
        version = self.store.version_of(AggregateType.RESULT_RELEASE, release_id) + 1

        snapshot = {"stage": stage, "scored": scored}
        payload = {
            "version": version,
            "format_id": format_id,
            "stage": stage,
            "title": format_obj["title"],
            "rankings": _rankings_payload(scored),
            "freeze": freeze,
            "snapshot_digest": digest(snapshot),
            "store_head_before": self.store.head_hash,
            "published_by": published_by,
        }
        event = self._emit(
            EventType.RESULT_REPUBLISHED, AggregateType.RESULT_RELEASE, release_id,
            payload, published_at, f"发布{_stage_label(stage)}第 {version} 版",
            event_id=f"rel-{format_id}-v{version}",
        )
        # 申诉中只是状态快照，名次未确定，不重发证书与奖励
        if stage != ReleaseStage.UNDER_APPEAL:
            self._issue_certificates_and_awards(
                format_id, version, stage, scored,
                self._last_substantive_release(snap, format_id), published_at,
            )
        return event

    def lock_results(self, format_id: str, *, locked_by: str, locked_at: str) -> dict:
        snap = self._snapshots()
        entries = [e for e in snap.entries.entries.values() if e["format_id"] == format_id]
        scored = [self._score_entry(snap, e) for e in entries]
        blockers = [s["entry_id"] for s in scored if s["open_case_ids"] or s["under_appeal"]]
        if blockers:
            raise AdjudicationError(f"仍有未决待审/申诉，不能发布正式成绩：{blockers}")

        ranked = sorted(
            (s for s in scored if s["status"] == "ranked"),
            key=lambda s: (s["category"], s["final_seconds"]),
        )
        rank_by_category: dict[str, int] = {}
        for s in ranked:
            rank_by_category[s["category"]] = rank_by_category.get(s["category"], 0) + 1
            s["rank"] = rank_by_category[s["category"]]
        for s in scored:
            if s["status"] != "ranked":
                s["rank"] = None

        freeze = self._freeze(snap, format_id, scored)
        release_id = f"release-{format_id}"
        version = self.store.version_of(AggregateType.RESULT_RELEASE, release_id) + 1
        snapshot = {"stage": ReleaseStage.OFFICIAL, "scored": scored}
        payload = {
            "version": version,
            "format_id": format_id,
            "stage": ReleaseStage.OFFICIAL,
            "title": snap.formats.get(format_id)["title"],
            "rankings": _rankings_payload(scored),
            "freeze": freeze,
            "snapshot_digest": digest(snapshot),
            "store_head_before": self.store.head_hash,
            "published_by": locked_by,
        }
        event = self._emit(
            EventType.RESULT_LOCKED, AggregateType.RESULT_RELEASE, release_id,
            payload, locked_at, f"正式成绩锁定第 {version} 版",
            event_id=f"lock-{format_id}-v{version}",
        )
        self._issue_certificates_and_awards(
            format_id, version, ReleaseStage.OFFICIAL, scored,
            self._last_substantive_release(snap, format_id), locked_at,
        )
        return event

    @staticmethod
    def _last_substantive_release(snap, format_id: str) -> dict | None:
        """最近一次真正重发证书/奖励的发布（跳过申诉中状态快照）。"""
        for rel in reversed(snap.releases.history(format_id)):
            if rel["stage"] != ReleaseStage.UNDER_APPEAL:
                return rel
        return None

    def _issue_certificates_and_awards(self, format_id: str, release_version: int,
                                       stage: str, scored: list[dict], prior: dict | None,
                                       at: str) -> None:
        prior_places: dict[tuple[str, int], str] = {}
        if prior:
            for row in prior["rankings"]:
                prior_places[(row["category"], row["rank"])] = row["entry_id"]

        for s in sorted(scored, key=lambda x: (x.get("rank") is None, x.get("rank") or 0)):
            if s["status"] != "ranked":
                continue
            cert_id = f"cert-{s['entry_id']}-v{release_version}"
            prev_version = prior["version"] if prior else None
            prev_cert = f"cert-{s['entry_id']}-v{prev_version}" if prev_version else None
            self._emit(
                EventType.CERTIFICATE_ISSUED, AggregateType.CERTIFICATE, cert_id,
                {"format_id": format_id, "entry_id": s["entry_id"],
                 "category": s["category"], "rank": s["rank"],
                 "final_seconds": s["final_seconds"], "release_version": release_version,
                 "stage": stage, "supersedes_certificate": prev_cert},
                at, f"证书 {s['bib']} 第{s['rank']}名 v{release_version}",
                event_id=f"cert-event-{cert_id}",
            )
            # 仅前三名涉及奖励递补
            if s["rank"] <= 3:
                award_id = f"award-{format_id}-{s['category']}-p{s['rank']}"
                prior_entry = prior_places.get((s["category"], s["rank"])) if prior else None
                payload = {
                    "format_id": format_id, "category": s["category"], "place": s["rank"],
                    "entry_id": s["entry_id"], "athlete_ids": [a["athlete_id"] for a in s["athletes"]],
                    "release_version": release_version, "stage": stage,
                }
                summary = f"奖励 {s['category']} 第{s['rank']}名 v{release_version}"
                if prior_entry and prior_entry != s["entry_id"]:
                    payload["previous_entry_id"] = prior_entry
                    payload["reason"] = "改判后新版本递补"
                    summary = f"奖励递补：{prior_entry} → {s['entry_id']}"
                self._emit(
                    EventType.AWARD_REROLLED, AggregateType.AWARD, award_id,
                    payload, at, summary, event_id=f"award-event-{award_id}-v{release_version}",
                )

    # ================================================================
    # 视图
    # ================================================================

    def participant_view(self, entry_id: str) -> dict:
        """参赛者视图：分段时间、处罚理由、当前名次。"""
        snap = self._snapshots()
        entry = snap.entries.get(entry_id)
        if entry is None:
            raise AdjudicationError(f"未知报名：{entry_id}")
        scored = self._score_entry(snap, entry)
        releases = []
        for rel in snap.releases.history(entry["format_id"]):
            row = next((r for r in rel["rankings"] if r["entry_id"] == entry_id), None)
            releases.append({"release_version": rel["version"], "stage": rel["stage"],
                             "rank": row["rank"] if row else None,
                             "final_seconds": row["final_seconds"] if row else None,
                             "snapshot_digest": rel["snapshot_digest"]})
        return {
            "entry_id": entry_id, "bib": entry["bib"], "category": entry["category"],
            "team_kind": entry["team_kind"], "athletes": entry["athletes"],
            "partner_replacements": entry["replacements"],
            "segments": scored["segments"],
            "gross_seconds": scored.get("gross_seconds"),
            "medical_seconds": scored.get("medical_seconds"),
            "penalty_seconds": scored.get("penalty_seconds"),
            "adjustment_seconds": scored.get("adjustment_seconds"),
            "final_seconds": scored.get("final_seconds"),
            "status": scored["status"],
            "rank": scored.get("rank"),
            "penalties": scored["penalties"],
            "open_case_ids": scored["open_case_ids"],
            "releases": releases,
        }

    def referee_view(self, format_id: str) -> dict:
        """裁判视图：定位每个冲突的证据来源。"""
        snap = self._snapshots()
        cases_out = []
        for case_id, case in snap.cases.cases.items():
            if not case["anomalies"] or case["anomalies"][0].get("format_id") != format_id:
                continue
            cases_out.append({
                "case_id": case_id,
                "open": case["resolution"] is None,
                "anomalies": case["anomalies"],
                "resolution": case["resolution"],
            })
        cases_out.sort(key=lambda c: (not c["open"], c["case_id"]))
        return {"format_id": format_id, "cases": cases_out}

    def public_results(self, format_id: str) -> dict:
        """公开结果：最新版本名次 + 冻结资料 + 可独立复验的哈希。"""
        snap = self._snapshots()
        latest = snap.releases.latest(format_id)
        if latest is None:
            raise AdjudicationError(f"{format_id} 尚未发布任何成绩")
        return {
            "format_id": format_id,
            "stage": latest["stage"],
            "release_version": latest["version"],
            "published_at": latest["occurred_at"],
            "rankings": latest["rankings"],
            "freeze_digest": digest(latest["freeze"]),
            "snapshot_digest": latest["snapshot_digest"],
            "store_head": self.store.head_hash,
            "chain_intact": self.store.verify_chain(),
        }

    def verify_latest_release(self, format_id: str) -> dict:
        """重放事件重建最新发布，逐项比对哈希，供公开端独立验证。"""
        public = self.public_results(format_id)
        latest = self._snapshots().releases.latest(format_id)
        rebuilt_snapshot = {"stage": latest["stage"],
                            "scored": self._rebuild_scored(format_id)}
        ok = digest(rebuilt_snapshot) == latest["snapshot_digest"]
        return {"snapshot_digest_matches": ok, "chain_intact": self.store.verify_chain(),
                "freeze_digest": digest(latest["freeze"]), **public}

    def _rebuild_scored(self, format_id: str) -> list[dict]:
        snap = self._snapshots()
        entries = [e for e in snap.entries.entries.values() if e["format_id"] == format_id]
        scored = [self._score_entry(snap, e) for e in entries]
        ranked = sorted((s for s in scored if s["status"] == "ranked"),
                        key=lambda s: (s["category"], s["final_seconds"]))
        ranks: dict[str, int] = {}
        for s in ranked:
            ranks[s["category"]] = ranks.get(s["category"], 0) + 1
            s["rank"] = ranks[s["category"]]
        for s in scored:
            if s["status"] != "ranked":
                s["rank"] = None
        return scored


def _rankings_payload(scored: list[dict]) -> list[dict]:
    rows = []
    for s in sorted(scored, key=lambda x: (x["category"], x.get("rank") is None, x.get("rank") or 0)):
        rows.append({
            "entry_id": s["entry_id"], "bib": s["bib"], "category": s["category"],
            "rank": s.get("rank"), "status": s["status"],
            "gross_seconds": s.get("gross_seconds"),
            "medical_seconds": s.get("medical_seconds"),
            "penalty_seconds": s.get("penalty_seconds"),
            "adjustment_seconds": s.get("adjustment_seconds"),
            "final_seconds": s.get("final_seconds"),
            "open_case_ids": s["open_case_ids"],
            "under_appeal": s["under_appeal"],
        })
    return rows


def _stage_label(stage: str) -> str:
    return {ReleaseStage.PRELIMINARY: "初榜",
            ReleaseStage.UNDER_APPEAL: "申诉中状态",
            ReleaseStage.OFFICIAL: "正式成绩"}.get(stage, stage)
