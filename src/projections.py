"""事件投影：从事件流重建各聚合的当前/历史状态。

投影只读事件、不产生决策；决策规则集中在 :mod:`src.app`。每份成绩发布时
冻结的资料（赛制、组别、分枪、搭档资格、器械状态）均可由这些投影按
事件版本重放得到。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .contracts import (
    AggregateType,
    DeviceStatus,
    EventType,
    MedicalStatus,
)
from .validator import parse_timestamp


def _group(events: list[dict], aggregate_type: AggregateType) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e["aggregate_type"] == aggregate_type:
            out[e["aggregate_id"]].append(e)
    return dict(out)


class FormatProjection:
    """赛制：16 个交替环节（跑段/固定项目）与计时门序列。"""

    def __init__(self, events: list[dict]) -> None:
        self.formats: dict[str, dict] = {}
        for ev in events:
            if ev["event_type"] != EventType.FORMAT_REGISTERED:
                continue
            p = ev["payload"]
            self.formats[ev["aggregate_id"]] = {
                "format_id": ev["aggregate_id"],
                "title": p["title"],
                "segments": [dict(s) for s in p["segments"]],
                "rules_version": p["rules_version"],
                "penalty_seconds": dict(p.get("penalty_seconds", {})),
                "revision": p.get("revision", 1),
                "event_version": ev["version"],
                "registered_at": ev["occurred_at"],
            }

    def get(self, format_id: str) -> dict:
        try:
            return self.formats[format_id]
        except KeyError:
            raise KeyError(f"未知赛制：{format_id}")

    @staticmethod
    def segment_index(format_obj: dict, segment_code: str) -> int:
        for i, seg in enumerate(format_obj["segments"]):
            if seg["code"] == segment_code:
                return i
        raise KeyError(f"赛制 {format_obj['format_id']} 无环节 {segment_code}")


class EntryProjection:
    """报名：组别、分枪、赛道、个人/双人搭档及替换资格。"""

    def __init__(self, events: list[dict]) -> None:
        self.entries: dict[str, dict] = {}
        for ev in events:
            if ev["event_type"] == EventType.ENTRY_CONFIRMED:
                p = ev["payload"]
                self.entries[ev["aggregate_id"]] = {
                    "entry_id": ev["aggregate_id"],
                    "format_id": p["format_id"],
                    "wave_id": p["wave_id"],
                    "category": p["category"],
                    "team_kind": p.get("team_kind", "individual"),
                    "lane": p.get("lane"),
                    "bib": p["bib"],
                    "athletes": [dict(a) for a in p.get("athletes", [])],
                    "chip_ids": list(p.get("chip_ids", [])),
                    "device_ids": list(p.get("device_ids", [])),
                    "replacements": [],
                    "confirmed_at": ev["occurred_at"],
                }
            elif ev["event_type"] == EventType.PARTNER_REPLACED:
                entry = self.entries.get(ev["aggregate_id"])
                if entry:
                    entry["replacements"].append(dict(ev["payload"]))
                    # 替换后成员表中旧成员离队、新成员入队
                    p = ev["payload"]
                    entry["athletes"] = [
                        a for a in entry["athletes"] if a["athlete_id"] != p["outgoing_athlete_id"]
                    ]
                    entry["athletes"].append(
                        {"athlete_id": p["incoming_athlete_id"], "role": p.get("incoming_role", "partner")}
                    )

    def get(self, entry_id: str) -> dict:
        try:
            return self.entries[entry_id]
        except KeyError:
            raise KeyError(f"未知报名：{entry_id}")

    def by_bib(self, format_id: str, bib: str) -> dict | None:
        for e in self.entries.values():
            if e["format_id"] == format_id and e["bib"] == bib:
                return e
        return None

    def chip_owner(self, chip_id: str) -> dict | None:
        for e in self.entries.values():
            if chip_id in e["chip_ids"]:
                return e
        return None

    def device_owner(self, device_id: str) -> dict | None:
        for e in self.entries.values():
            if device_id in e["device_ids"]:
                return e
        return None


class DeviceProjection:
    """器械：按真实时间记录状态，并记录注册时的绑定赛道。"""

    def __init__(self, events: list[dict]) -> None:
        self.devices: dict[str, dict] = {}
        for ev in events:
            if ev["event_type"] == EventType.DEVICE_REGISTERED:
                p = ev["payload"]
                self.devices[ev["aggregate_id"]] = {
                    "device_id": ev["aggregate_id"],
                    "apparatus_code": p["apparatus_code"],
                    "lane": p.get("lane"),
                    "status": DeviceStatus.NOMINAL,
                    "timeline": [],
                }
            elif ev["event_type"] == EventType.DEVICE_STATUS_RECORDED:
                dev = self.devices.get(ev["aggregate_id"])
                if dev:
                    p = ev["payload"]
                    dev["timeline"].append(
                        {"at": parse_timestamp(ev["occurred_at"]), "status": p["status"], "note": p.get("note", "")}
                    )

    def status_at(self, device_id: str, moment: datetime) -> DeviceStatus:
        """器械在指定真实时刻的状态（取该时刻之前最后一条状态记录）。"""
        dev = self.devices[device_id]
        status = DeviceStatus.NOMINAL
        for item in sorted(dev["timeline"], key=lambda x: x["at"]):
            if item["at"] <= moment:
                status = item["status"]
            else:
                break
        return DeviceStatus(status)

    def get(self, device_id: str) -> dict:
        return self.devices[device_id]


class WaveProjection:
    """分枪：真实发枪时间，迟到证据按发枪之后的真实时间归位。"""

    def __init__(self, events: list[dict]) -> None:
        self.waves: dict[tuple[str, str], dict] = {}
        for ev in events:
            if ev["event_type"] == EventType.WAVE_STARTED:
                p = ev["payload"]
                self.waves[(ev["aggregate_id"], p["wave_id"])] = {
                    "format_id": ev["aggregate_id"],
                    "wave_id": p["wave_id"],
                    "started_at": parse_timestamp(ev["occurred_at"]),
                }

    def start(self, format_id: str, wave_id: str) -> datetime:
        return self.waves[(format_id, wave_id)]["started_at"]


class RaceProjection:
    """单个报名的证据与组装结果：按真实发生时间排序。"""

    def __init__(self, events: list[dict]) -> None:
        groups = _group(events, AggregateType.COMPETITION_ENTRY)
        self.races: dict[str, dict] = {}
        for entry_id, stream in groups.items():
            splits, assembled, rejected = [], [], []
            for ev in stream:
                if ev["event_type"] == EventType.SPLIT_RECORDED:
                    splits.append({**ev["payload"], "recorded_event": ev["event_id"], "occurred_at": ev["occurred_at"]})
                elif ev["event_type"] == EventType.SEGMENT_ASSEMBLED:
                    assembled.append({**ev["payload"], "event_id": ev["event_id"], "version": ev["version"]})
                elif ev["event_type"] == EventType.EVIDENCE_REJECTED:
                    rejected.append({**ev["payload"], "event_id": ev["event_id"]})
            splits.sort(key=lambda s: parse_timestamp(s["observed_at"]))
            self.races[entry_id] = {"splits": splits, "assembled": assembled, "rejected": rejected}

    def for_entry(self, entry_id: str) -> dict:
        return self.races.setdefault(entry_id, {"splits": [], "assembled": [], "rejected": []})


class MedicalProjection:
    """医疗停止优先于计时流程：停止期间的读数不得组装。"""

    def __init__(self, events: list[dict]) -> None:
        groups = _group(events, AggregateType.MEDICAL_HOLD)
        self.holds: dict[str, dict] = {}
        for hold_id, stream in groups.items():
            status, intervals, start = None, [], None
            for ev in sorted(stream, key=lambda e: parse_timestamp(e["occurred_at"])):
                if ev["event_type"] == EventType.MEDICAL_STOP_ISSUED:
                    status = MedicalStatus.STOPPED
                    start = parse_timestamp(ev["occurred_at"])
                elif ev["event_type"] == EventType.MEDICAL_RESUME_AUTHORIZED:
                    if status == MedicalStatus.STOPPED and start is not None:
                        intervals.append((start, parse_timestamp(ev["occurred_at"])))
                    status = MedicalStatus.RESUMED
                    start = None
            self.holds[hold_id] = {"entry_id": stream[0]["payload"]["entry_id"], "status": status, "intervals": intervals}

    def is_stopped_at(self, entry_id: str, moment: datetime) -> bool:
        for hold in self.holds.values():
            if hold["entry_id"] != entry_id:
                continue
            if hold["status"] == MedicalStatus.STOPPED:
                return True
            for start, end in hold["intervals"]:
                if start <= moment <= end:
                    return True
        return False


class CaseProjection:
    """待审案件：校验异常只进待审，处罚只能来自签署的认定。"""

    def __init__(self, events: list[dict]) -> None:
        groups = _group(events, AggregateType.REVIEW_CASE)
        rulings = _group(events, AggregateType.RULING)
        superseded = {
            ev["payload"]["supersedes_ruling_id"]
            for stream in rulings.values() for ev in stream
            if ev["event_type"] == EventType.RULING_SIGNED and ev["payload"].get("supersedes_ruling_id")
        }
        self.cases: dict[str, dict] = {}
        for case_id, stream in groups.items():
            anomalies = [
                {**ev["payload"], "flagged_at": ev["occurred_at"], "event_id": ev["event_id"]}
                for ev in stream
                if ev["event_type"] == EventType.ANOMALY_FLAGGED
            ]
            candidates = [
                ev for stream in rulings.values() for ev in stream
                if ev["event_type"] == EventType.RULING_SIGNED
                and ev["payload"].get("case_id") == case_id
                and ev["aggregate_id"] not in superseded
            ]
            resolution = None
            if candidates:
                ev = max(candidates, key=lambda e: parse_timestamp(e["occurred_at"]))
                resolution = {**ev["payload"], "ruling_id": ev["aggregate_id"], "signed_at": ev["occurred_at"]}
            self.cases[case_id] = {"anomalies": anomalies, "resolution": resolution}

    def open_for_entry(self, entry_id: str) -> list[str]:
        return [
            cid
            for cid, c in self.cases.items()
            if c["resolution"] is None
            and any(a.get("entry_id") == entry_id for a in c["anomalies"])
        ]


class RulingProjection:
    def __init__(self, events: list[dict]) -> None:
        groups = _group(events, AggregateType.RULING)
        self.rulings: dict[str, dict] = {
            rid: stream[-1]["payload"] | {
                "ruling_id": rid,
                "signed_at": stream[-1]["occurred_at"],
                "event_id": stream[-1]["event_id"],
            }
            for rid, stream in groups.items()
        }
        self.by_entry: dict[str, list[dict]] = defaultdict(list)
        for r in self.rulings.values():
            self.by_entry[r["entry_id"]].append(r)

    def for_entry(self, entry_id: str) -> list[dict]:
        return list(self.by_entry.get(entry_id, []))


class AppealProjection:
    def __init__(self, events: list[dict]) -> None:
        groups = _group(events, AggregateType.APPEAL)
        self.appeals: dict[str, dict] = {}
        for aid, stream in groups.items():
            filed, decision = None, None
            for ev in stream:
                if ev["event_type"] == EventType.APPEAL_FILED:
                    filed = ev
                elif ev["event_type"] == EventType.APPEAL_DECIDED:
                    decision = ev
            self.appeals[aid] = {
                "entry_id": filed["payload"]["entry_id"],
                "ruling_id": filed["payload"]["ruling_id"],
                "status": decision["payload"]["outcome"] if decision else "filed",
                "new_ruling_id": decision["payload"].get("new_ruling_id") if decision else None,
            }

    def open_for_entry(self, entry_id: str) -> bool:
        return any(a["entry_id"] == entry_id and a["status"] == "filed" for a in self.appeals.values())


class ReleaseProjection:
    """成绩发布：初榜/申诉中/正式，逐版本保留规则与证据快照。"""

    def __init__(self, events: list[dict]) -> None:
        groups = _group(events, AggregateType.RESULT_RELEASE)
        self.releases: dict[str, list[dict]] = defaultdict(list)
        for rid, stream in groups.items():
            for ev in stream:
                if ev["event_type"] in (EventType.RESULT_REPUBLISHED, EventType.RESULT_LOCKED):
                    self.releases[ev["payload"]["format_id"]].append(
                        {**ev["payload"], "event_id": ev["event_id"], "occurred_at": ev["occurred_at"], "release_id": rid}
                    )
        for ver_list in self.releases.values():
            ver_list.sort(key=lambda r: r["version"])

    def latest(self, format_id: str) -> dict | None:
        versions = self.releases.get(format_id)
        return versions[-1] if versions else None

    def history(self, format_id: str) -> list[dict]:
        return list(self.releases.get(format_id, []))
